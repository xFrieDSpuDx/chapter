from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call, patch

from abs_kids_player.api import AudiobookshelfClient, client_from_config
from abs_kids_player.config import AppConfig
from abs_kids_player.download_store import DownloadedBook, book_directory, lock_download
from abs_kids_player.gstreamer_runtime import GStreamerRuntime
from abs_kids_player.library_cache import save_cached_books
from abs_kids_player.models import AudioTrack, Book, PlaybackSession
from abs_kids_player.playback import (
    PREROLL_VOLUME_FLOOR,
    SILENCE_VOLUME,
    SPOKEN_NAVIGATION_DUCK_FACTOR,
    STATE_CHANGE_TIMEOUT_SECONDS,
    GStreamerPlayback,
    PlaybackError,
    smart_resume_rewind_seconds,
)
from abs_kids_player.playback_start import prepare_book_playback
from abs_kids_player.playback_timing import PlaybackTiming
from abs_kids_player.podcast import PodcastProgressClient, podcast_progress, podcast_progress_key
from abs_kids_player.preroll_probe import PrerollMilestones
from abs_kids_player.progress_sync import ProgressSync, read_pending_progress


class FakePlaybin:
    def __init__(self, reject_mute: bool = False) -> None:
        self.reject_mute = reject_mute
        self.properties: dict[str, object] = {}
        self.volume_values: list[float] = []
        self.states: list[object] = []
        self.seeks: list[tuple[object, object, int]] = []
        self.rate_seeks: list[tuple[float, object, object, object, int, object, int]] = []
        self.events: list[tuple[str, object]] = []
        self.position = 0
        self.query_position_calls = 0

    def set_property(self, name: str, value: object) -> None:
        if name == "mute" and self.reject_mute:
            raise TypeError("unknown property mute")
        self.properties[name] = value
        if name == "volume":
            self.volume_values.append(float(value))
            self.events.append(("volume", float(value)))

    def set_state(self, state: object) -> None:
        self.states.append(state)
        self.events.append(("state", state))

    def get_state(self, timeout: int) -> tuple[str, str, None]:
        self.events.append(("get_state", timeout))
        return FakeGst.StateChangeReturn.SUCCESS, FakeGst.State.PAUSED, None

    def query_position(self, _format: object) -> tuple[bool, int]:
        self.query_position_calls += 1
        return True, self.position

    def seek_simple(self, format_: object, flags: object, position: int) -> bool:
        self.seeks.append((format_, flags, position))
        self.events.append(("seek", position))
        return True

    def seek(
        self,
        rate: float,
        format_: object,
        flags: object,
        start_type: object,
        start: int,
        stop_type: object,
        stop: int,
    ) -> bool:
        if not -(1 << 63) <= stop < (1 << 63):
            raise OverflowError("seek stop is outside the signed 64-bit range")
        self.rate_seeks.append((rate, format_, flags, start_type, start, stop_type, stop))
        self.events.append(("rate-seek", start))
        return True


class FakeSink:
    def __init__(self) -> None:
        self.properties: dict[str, object] = {}

    def set_property(self, name: str, value: object) -> None:
        self.properties[name] = value


class FakeGst:
    SECOND = 1
    CLOCK_TIME_NONE = (1 << 64) - 1

    class ElementFactory:
        @staticmethod
        def make(_element_type: str, _name: str) -> FakePlaybin:
            return FakePlaybin()

    class State:
        NULL = "null"
        PAUSED = "paused"
        PLAYING = "playing"

    class StateChangeReturn:
        SUCCESS = "success"
        ASYNC = "async"
        FAILURE = "failure"
        NO_PREROLL = "no-preroll"

    class Format:
        TIME = "time"

    class SeekFlags:
        FLUSH = 1
        KEY_UNIT = 2
        ACCURATE = 4

    class SeekType:
        SET = "set"
        NONE = "none"


class FakeGstWithSink(FakeGst):
    sinks: list[FakeSink] = []

    class ElementFactory:
        @staticmethod
        def make(element_type: str, _name: str):
            if element_type == "alsasink":
                sink = FakeSink()
                FakeGstWithSink.sinks.append(sink)
                return sink
            return FakePlaybin()


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float, float, str, float]] = []

    def update_progress(
        self,
        item_id: str,
        current_time: float,
        duration: float,
        session_id: str = "",
        time_listened: float = 0,
    ) -> None:
        self.calls.append((item_id, current_time, duration, session_id, time_listened))


class PlaybackTest(unittest.TestCase):
    def test_first_book_reuses_the_initialised_runtime_without_preopening_audio(self) -> None:
        runtime = GStreamerRuntime()
        playback = GStreamerPlayback(runtime=runtime)
        session = SimpleNamespace(
            duration=300, current_time=12,
            tracks=[SimpleNamespace(start_offset=0, duration=300, url="file:///book.mp3")],
        )
        gst = FakeGst()
        with (
            patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=gst) as initialise,
            patch.object(gst.ElementFactory, "make", wraps=gst.ElementFactory.make) as make_element,
            patch("abs_kids_player.playback.time.sleep"),
        ):
            runtime.load()
            make_element.assert_not_called()
            self.assertIsNone(playback.playbin)
            first = PlaybackTiming(1, 1, "continue")
            playback.start(session, FakeClient(), timing=first)
            self.assertIs(first.first_pipeline_since_restart, True)
            self.assertTrue(playback.is_playing)
            make_element.assert_any_call("playbin", "chapter-player")
            later = PlaybackTiming(2, 2, "continue")
            playback.start(session, FakeClient(), timing=later)
            self.assertIs(later.first_pipeline_since_restart, False)
            initialise.assert_called_once_with(None)

    def test_start_at_1x_skips_zero_seek_and_second_wait_after_preroll_timeout(self) -> None:
        playback = GStreamerPlayback()
        session = SimpleNamespace(
            duration=300,
            current_time=0,
            tracks=[SimpleNamespace(start_offset=0, duration=300, url="http://example.test/book.mp3")],
        )
        timing = PlaybackTiming(1.0, 1.0, "start")

        with (
            patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=FakeGst()),
            patch("abs_kids_player.playback.time.sleep"),
            patch.object(
                FakePlaybin,
                "get_state",
                return_value=(FakeGst.StateChangeReturn.ASYNC, FakeGst.State.NULL, FakeGst.State.PAUSED),
            ) as state_wait,
        ):
            playback.start(session, FakeClient(), start_over=True, timing=timing)

        state_wait.assert_called_once_with(STATE_CHANGE_TIMEOUT_SECONDS * FakeGst.SECOND)
        self.assertEqual(playback.playbin.seeks, [])
        self.assertEqual(playback.playbin.rate_seeks, [])
        self.assertEqual(timing.preroll_wait, "timeout")
        self.assertIsNotNone(timing.preroll_finished_at)
        self.assertEqual(timing.seek_wait, "skipped")
        self.assertIsNone(timing.seek_at)
        self.assertIsNone(timing.seek_finished_at)
        self.assertEqual(playback.playbin.states[-1], FakeGst.State.PLAYING)

    def test_cancel_interrupts_zero_offset_preroll_before_playing_or_resetting_progress(self):
        playback = GStreamerPlayback()
        session = SimpleNamespace(current_time=0, tracks=[AudioTrack(1, 0, 300, "https://books.test/book.m4b", "")])
        waited = []
        def pending(timeout):
            waited.append(timeout)
            return FakeGst.StateChangeReturn.ASYNC, FakeGst.State.PAUSED, None
        with (patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=FakeGst()),
              patch.object(FakePlaybin, "get_state", side_effect=pending),
              patch.object(playback, "request_progress_sync") as sync):
            playback.start(session, FakeClient(), start_over=True, cancelled=lambda: bool(waited))
        self.assertEqual(waited, [0])
        self.assertNotIn(FakeGst.State.PLAYING, playback.playbin.states)
        sync.assert_not_called()

    def test_cancel_during_fade_silences_the_pipeline_without_saving_start_over(self):
        engine = GStreamerPlayback(progress_sync=Mock())
        session = SimpleNamespace(current_time=0, tracks=[AudioTrack(1, 0, 300, "https://books.test/book.m4b", "")])
        def cancelled():
            return bool(engine.playbin and any(v > PREROLL_VOLUME_FLOOR for v in engine.playbin.volume_values))
        with (patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=FakeGst()),
              patch("abs_kids_player.playback.time.sleep"), patch.object(engine, "request_progress_sync") as sync):
            engine.start(session, FakeClient(), start_over=True, cancelled=cancelled)
        self.assertTrue(cancelled())
        self.assertEqual(engine.playbin.volume_values[-1], SILENCE_VOLUME)
        sync.assert_not_called()

    def test_stop_saves_local_or_streamed_position_and_releases_without_completing(self):
        for downloaded in (False, True):
            for paused in (False, True):
                with self.subTest(downloaded=downloaded, paused=paused):
                    engine = GStreamerPlayback()
                    engine.Gst, engine.playbin = FakeGst(), FakePlaybin()
                    pipeline = engine.playbin
                    pipeline.position = 11 * engine.Gst.SECOND
                    engine.session = PlaybackSession("session", "book", "Title", "", 300, 107, "",
                        [AudioTrack(1, 0, 100, "first", ""), AudioTrack(2, 100, 200, "second", "")],
                        [], progress_account="reader", downloaded=downloaded)
                    engine.track_index = 1
                    engine.current_time = 107
                    engine.is_playing, engine.soft_paused = not paused, paused
                    engine.client = Mock(progress_account="reader")
                    engine.progress_sync = Mock()
                    lock = Mock()
                    engine._download_lock = lock if downloaded else None
                    expected = 107 if paused else 111
                    with patch("abs_kids_player.playback.save_last_playback") as save:
                        self.assertEqual(engine.stop_listening(), expected)
                    save.assert_called_once_with("book", "Title", expected, episode_id="", account="reader")
                    self.assertEqual(engine.progress_sync.record.call_args.args[:4], ("reader", "book", expected, 300))
                    self.assertEqual(pipeline.query_position_calls, int(not paused))
                    self.assertEqual(pipeline.properties["volume"], SILENCE_VOLUME)
                    self.assertEqual(pipeline.states[-1], engine.Gst.State.NULL)
                    self.assertIsNone(engine.completed_session)
                    self.assertIsNone(engine.session)
                    self.assertEqual(lock.close.call_count, int(downloaded))

    def test_closing_a_paused_book_does_not_upload_its_old_place_again(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {
            "ABS_KIDS_PLAYER_STATE_PATH": str(Path(folder) / "state.json")
        }):
            sync = ProgressSync()
            engine = GStreamerPlayback(progress_sync=sync)
            engine.Gst, engine.playbin = FakeGst(), FakePlaybin()
            engine.session = PlaybackSession("", "book", "Title", "", 300, 35, "", [], [], progress_account="reader")
            engine.client = Mock(progress_account="reader")
            engine.current_time = 35
            engine.pause(keep_audio_alive=False)
            saved = read_pending_progress()[("reader", "book", "")]
            engine.stop_listening()
            self.assertEqual(read_pending_progress()[saved.key], saved)

    def test_browser_takeover_pauses_audio_at_remote_position_without_an_echo_write(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {
            "ABS_KIDS_PLAYER_STATE_PATH": str(Path(folder) / "state.json")
        }):
            config = AppConfig(server_url="https://books.example.com", token="test-token")
            client = client_from_config(config)
            session = PlaybackSession("stream", "book", "Title", "", 300, 18, "",
                                      [AudioTrack(1, 0, 300, "http://books.test/audio", "Book")], [],
                                      progress_account=client.progress_account, server_checkpoint=(18, 100))
            queue = ProgressSync()
            engine = GStreamerPlayback(progress_sync=queue)
            with patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=FakeGst()), patch("time.sleep"):
                engine.start(session, client)
            engine.current_time = 26
            engine.request_progress_sync()
            with (
                patch("abs_kids_player.api.client_from_config", return_value=client),
                patch.object(client, "get_progress", return_value={"currentTime": 9, "lastUpdate": 200}),
                patch.object(client, "update_progress") as upload,
                patch("abs_kids_player.progress_sync.threading.Thread") as worker,
            ):
                queue.pump(config)
                task = worker.call_args.kwargs
                task["target"](*task["args"])
                engine.poll()
                self.assertFalse(engine.is_playing)
                self.assertEqual(engine.current_time, 9)
                self.assertEqual(engine.playbin.seeks[-1][-1], 9)
                engine.stop_listening()
                self.assertFalse(read_pending_progress())
                upload.assert_not_called()

    def test_stop_releases_the_download_even_when_one_cleanup_step_fails(self):
        for failure in ("progress", "timing", "pipeline"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                book = Path(directory) / "book"
                engine = GStreamerPlayback(progress_sync=Mock())
                pipeline = engine.playbin = Mock()
                probe = engine._startup_probe = Mock()
                engine.Gst = FakeGst()
                engine.session = Mock()
                engine.is_playing = True
                engine.last_sync = 1
                with lock_download(book, shared=True) as playing_lock:
                    engine._download_lock = playing_lock
                    with patch.object(engine, "request_progress_sync") as sync:
                        failing_step = {"progress": sync, "timing": probe.timing.observe_output,
                                        "pipeline": pipeline.set_state}[failure]
                        failing_step.side_effect = OSError(failure)
                        with self.assertRaisesRegex(OSError, failure):
                            engine.stop()
                    self.assertTrue(playing_lock.closed)
                    with lock_download(book, shared=False):
                        engine.stop()  # Cleanup can be called again.
                sync.assert_called_once_with()
                probe.timing.observe_output.assert_called_once_with("output_interrupted")
                pipeline.set_state.assert_called_once_with(FakeGst.State.NULL)
                self.assertFalse(engine.is_playing)
                self.assertIsNone(engine.session)
                self.assertIsNone(engine.playbin)

    def test_reports_gstreamer_state_wait_outcomes(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        outcomes = FakeGst.StateChangeReturn
        for returned, expected in (
            (outcomes.SUCCESS, "completed"),
            (outcomes.ASYNC, "timeout"),
            (outcomes.FAILURE, "failed"),
            (outcomes.NO_PREROLL, "no_preroll"),
        ):
            with (
                self.subTest(returned=returned),
                patch.object(playback.playbin, "get_state", return_value=(returned, None, None)),
            ):
                self.assertEqual(playback.wait_for_state_change(), expected)

    def test_start_sets_volume_floor_before_preroll(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        session = SimpleNamespace(
            library_item_id="book-1",
            duration=300,
            current_time=42,
            tracks=[SimpleNamespace(start_offset=0, duration=300, url="http://example.test/book.mp3")],
        )

        with patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=FakeGst()):
            with patch("abs_kids_player.playback.time.sleep"):
                playback.start(session, FakeClient())

        self.assertAlmostEqual(playback.playbin.volume_values[0], PREROLL_VOLUME_FLOOR)
        self.assertEqual(playback.playbin.states[0], FakeGst.State.NULL)
        self.assertEqual(playback.playbin.states[-1], FakeGst.State.PLAYING)
        self.assertEqual(playback.playbin.seeks[-1], (FakeGst.Format.TIME, 3, 42))
        playing_index = playback.playbin.events.index(("state", FakeGst.State.PLAYING))
        preroll_seek_index = playback.playbin.events.index(("seek", 42))
        first_audible_volume_index = next(
            index
            for index, event in enumerate(playback.playbin.events)
            if event[0] == "volume" and event[1] > PREROLL_VOLUME_FLOOR
        )
        self.assertLess(preroll_seek_index, playing_index)
        self.assertLess(playing_index, first_audible_volume_index)
        self.assertIn("audio-filter", playback.playbin.properties)

    def test_already_cancelled_start_never_creates_a_pipeline(self) -> None:
        playback = GStreamerPlayback()
        session = SimpleNamespace(
            library_item_id="book-1",
            duration=300,
            current_time=0,
            tracks=[SimpleNamespace(start_offset=0, duration=300, url="http://example.test/book.mp3")],
        )

        with patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=FakeGst()):
            playback.start(session, FakeClient(), cancelled=lambda: True)

        self.assertIsNone(playback.playbin)
        self.assertFalse(playback.is_playing)

    def test_continue_measures_steps_before_preroll(self) -> None:
        playback = GStreamerPlayback()
        playback.set_volume_state(0, True)
        playback.Gst = FakeGst()
        playback.playbin = old_playbin = FakePlaybin()
        playbin = FakePlaybin()
        session = SimpleNamespace(
            duration=300,
            current_time=42,
            tracks=[SimpleNamespace(start_offset=0, duration=300, url="/book.mp3")],
        )
        client = AudiobookshelfClient("https://example.test", "test-token")
        now = 10.0
        timing = PlaybackTiming(9.0, 9.1, "continue", clock=lambda: now, pipeline_at=now)

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        def stop_old_playbin(state: str) -> None:
            advance(0.4)
            old_playbin.states.append(state)

        def prepare_media_url(_url: str) -> str:
            advance(0.6)
            return "https://example.test/book.mp3?token=test-token"

        def create_element(kind: str, _name: str) -> FakePlaybin:
            if kind == "playbin":
                advance(0.7)
                return playbin
            advance(0.2)
            return FakePlaybin()

        set_property = playbin.set_property

        def set_timed_property(name: str, value: object) -> None:
            advance({"audio-sink": 0.15, "uri": 0.05}.get(name, 0.0))
            set_property(name, value)

        trace = StringIO()

        def initialize_gst(_args: object) -> None:
            print("native registry trace", file=trace)
            advance(4.0)

        gst = FakeGst()
        gst.init = Mock(side_effect=initialize_gst)
        gi = ModuleType("gi")
        gi.require_version = Mock(side_effect=lambda *_: advance(0.1))
        repository = ModuleType("gi.repository")
        repository.Gst = gst
        with (
            patch.dict("os.environ", {"CHAPTER_TRACE_GST_INIT": "1"}),
            patch("abs_kids_player.playback_timing.os.getpid", return_value=1234),
            redirect_stderr(trace),
            patch.dict("sys.modules", {"gi": gi, "gi.repository": repository}),
            patch.object(old_playbin, "set_state", side_effect=stop_old_playbin),
            patch.object(gst.ElementFactory, "make", side_effect=create_element),
            patch.object(client, "authenticated_media_url", side_effect=prepare_media_url),
            patch.object(playbin, "set_property", side_effect=set_timed_property),
            patch("abs_kids_player.playback.time.sleep", side_effect=advance),
        ):
            playback.start(session, client, timing=timing)

        for step, seconds in {
            "stop_previous": 0.4,
            "bindings": 0.1,
            "init": 4.0,
            "playbin_create": 0.7,
            "tempo_filter": 0.2,
            "session_setup": 0.0,
            "media_url": 0.6,
            "reset_to_null": 0.0,
            "audio_sink_setup": 0.15,
            "set_uri": 0.05,
            "preroll_probe_setup": 0.0,
        }.items():
            with self.subTest(step=step):
                self.assertAlmostEqual(timing.pipeline_setup_seconds[step], seconds)
        self.assertAlmostEqual(timing.preroll_at, 16.2)
        self.assertEqual(old_playbin.states, [FakeGst.State.NULL])
        self.assertEqual(playbin.seeks, [(FakeGst.Format.TIME, 3, 42)])
        self.assertEqual(
            trace.getvalue().splitlines(),
            [
                "GStreamer init phase=begin pid=1234",
                "native registry trace",
                "GStreamer init phase=end pid=1234 status=completed",
            ],
        )

    def test_continue_at_track_boundary_measures_preroll_seek_and_output(self) -> None:
        playback = GStreamerPlayback()
        session = SimpleNamespace(
            duration=300,
            current_time=10,
            tracks=[AudioTrack(1, 0, 10, "http://example.test/intro.mp3", "Intro"),
                    AudioTrack(2, 10, 290, "http://example.test/book.mp3", "Book")],
        )
        now = 10.0

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        real_set_state = FakePlaybin.set_state
        real_seek = FakePlaybin.seek_simple
        milestones = PrerollMilestones(source_setup_at=10.05, first_source_buffer_at=10.25)
        probe = Mock()
        probe.finish.return_value = milestones

        def set_state(playbin: FakePlaybin, state: str) -> None:
            if state == FakeGst.State.PAUSED:
                probe.attach.assert_called_once_with()
                advance(0.2)
            real_set_state(playbin, state)

        def seek(playbin: FakePlaybin, format_: str, flags: int, position: int) -> None:
            probe.finish.assert_called_once_with(now)
            real_seek(playbin, format_, flags, position)

        waits = iter(
            (
                (0.1, FakeGst.StateChangeReturn.SUCCESS),
                (5.0, FakeGst.StateChangeReturn.ASYNC),
            )
        )

        def get_state(_timeout: int) -> tuple[str, str, None]:
            seconds, state = next(waits)
            advance(seconds)
            return state, FakeGst.State.PAUSED, None

        timing = PlaybackTiming(9.0, 9.1, "continue", clock=lambda: now, pipeline_at=now)
        sink = Mock()
        sink.get_property.return_value.get_value.side_effect = lambda _key: int(now >= 15.52)
        with (
            patch.dict("os.environ", {"CHAPTER_TRACE_SOURCE_READS": "1"}),
            patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=FakeGst()),
            patch("abs_kids_player.playback.find_audio_sink", return_value=sink),
            patch("abs_kids_player.playback.time.sleep", side_effect=advance),
            patch("abs_kids_player.playback.PrerollProbe", return_value=probe) as probe_type,
            patch.object(FakePlaybin, "set_state", autospec=True, side_effect=set_state),
            patch.object(FakePlaybin, "seek_simple", autospec=True, side_effect=seek),
            patch.object(FakePlaybin, "get_state", side_effect=get_state) as state_wait,
        ):
            playback.start(session, FakeClient(), timing=timing)

        probe_type.assert_called_once_with(
            playback.Gst, playback.playbin, timing.clock, trace_source_reads=True
        )
        self.assertAlmostEqual(timing.preroll_at, 10.0)
        self.assertAlmostEqual(timing.pause_returned_at, 10.2)
        self.assertAlmostEqual(timing.preroll_finished_at, 10.3)
        self.assertEqual(timing.preroll_wait, "completed")
        self.assertIs(timing.preroll_milestones, milestones)
        self.assertAlmostEqual(timing.seek_at, 10.3)
        self.assertAlmostEqual(timing.seek_finished_at, 15.3)
        self.assertEqual(timing.seek_wait, "timeout")
        self.assertEqual(playback.playbin.seeks, [(FakeGst.Format.TIME, 3, 0)])
        self.assertEqual(state_wait.call_count, 2)
        self.assertAlmostEqual(timing.gain_at, 15.5)
        self.assertEqual(timing.status, "output_observed")
        self.assertGreater(timing.rendered_at, timing.gain_at)
        self.assertLess(timing.rendered_at, now)
        self.assertTrue(playback.is_playing)

    def test_a_muted_start_does_not_report_an_audio_render(self) -> None:
        playback = GStreamerPlayback()
        playback.set_volume_state(0, True)
        timing = PlaybackTiming(9.0, 9.1, "start")
        session = SimpleNamespace(
            duration=300,
            current_time=0,
            tracks=[SimpleNamespace(start_offset=0, duration=300, url="http://example.test/book.mp3")],
        )

        with (
            patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=FakeGst()),
            patch("abs_kids_player.playback.find_audio_sink") as find_sink,
            patch("abs_kids_player.playback.time.sleep"),
        ):
            playback.start(session, FakeClient(), timing=timing)

        find_sink.assert_not_called()
        self.assertEqual(timing.status, "muted")
        self.assertIsNone(timing.rendered_at)

    def test_start_at_faster_playback_rate_still_seeks_at_zero(self) -> None:
        playback = GStreamerPlayback()
        playback.set_playback_rate(1.5)
        session = SimpleNamespace(
            library_item_id="book-1",
            duration=300,
            current_time=0,
            tracks=[SimpleNamespace(start_offset=0, duration=300, url="http://example.test/book.mp3")],
        )

        with patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=FakeGst()):
            with patch("abs_kids_player.playback.time.sleep"):
                playback.start(session, FakeClient(), start_over=True)

        self.assertEqual(playback.playback_rate, 1.5)
        self.assertEqual(playback.playbin.seeks, [])
        self.assertEqual(
            playback.playbin.rate_seeks[-1],
            (1.5, FakeGst.Format.TIME, 3, FakeGst.SeekType.SET, 0, FakeGst.SeekType.NONE, -1),
        )
        self.assertEqual(playback.playbin.events.count(("get_state", STATE_CHANGE_TIMEOUT_SECONDS)), 2)

    def test_playback_rate_change_applies_to_active_session(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        playback.session = SimpleNamespace(
            duration=300,
            tracks=[SimpleNamespace(start_offset=0, duration=300, url="http://example.test/book.mp3")],
        )
        playback.current_time = 42
        playback.playbin.position = 45
        playback.is_playing = True

        playback.set_playback_rate(2.0)

        self.assertEqual(playback.playback_rate, 2.0)
        self.assertEqual(playback.current_time, 45)
        self.assertEqual(
            playback.playbin.rate_seeks[-1],
            (2.0, FakeGst.Format.TIME, 3, FakeGst.SeekType.SET, 45, FakeGst.SeekType.NONE, -1),
        )

        playback.set_playback_rate(1.0)

        self.assertEqual(playback.playbin.seeks[-1], (FakeGst.Format.TIME, 3, 45))
        with patch.object(playback, "seek_track", return_value=False):
            with self.assertRaisesRegex(PlaybackError, "Could not change playback speed"):
                playback.set_playback_rate(1.5)
        self.assertEqual(playback.playback_rate, 1.0)

    def test_native_seek_exception_keeps_speed_and_listening_position(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        playback.session = SimpleNamespace(tracks=[SimpleNamespace(start_offset=60)])
        playback.current_time = 82
        playback.playbin.position = 24
        playback.is_playing = True

        with patch.object(playback.playbin, "seek", side_effect=OverflowError("signed stop")):
            with self.assertRaisesRegex(PlaybackError, "signed stop"):
                playback.set_playback_rate(2.0)

        self.assertEqual((playback.playback_rate, playback.current_time), (1.0, 82))
        playback.playbin.position = FakeGst.CLOCK_TIME_NONE
        playback.set_playback_rate(1.5)
        self.assertEqual(playback.playbin.rate_seeks[-1][4], 22)
        self.assertEqual(playback.current_time, 82)

    def test_start_uses_bluetooth_alsa_sink_when_connected(self) -> None:
        playback = GStreamerPlayback()
        playback.set_bluetooth_output_enabled(True, "AA:BB:CC:DD:EE:FF")
        session = SimpleNamespace(
            library_item_id="book-1",
            duration=300,
            current_time=42,
            tracks=[SimpleNamespace(start_offset=0, duration=300, url="http://example.test/book.mp3")],
        )
        FakeGstWithSink.sinks = []
        device = "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp"

        with patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=FakeGstWithSink()):
            with patch("abs_kids_player.playback.preferred_bluetooth_alsa_device", return_value=device) as selected:
                with patch("abs_kids_player.playback.time.sleep"):
                    playback.start(session, FakeClient())

        selected.assert_called_once_with("AA:BB:CC:DD:EE:FF")
        self.assertEqual(playback.audio_output_device, device)
        self.assertEqual(len(FakeGstWithSink.sinks), 1)
        self.assertEqual(FakeGstWithSink.sinks[0].properties["device"], device)
        self.assertIs(playback.playbin.properties["audio-sink"], FakeGstWithSink.sinks[0])

    def test_start_uses_internal_speaker_without_probing_bluetooth(self) -> None:
        playback = GStreamerPlayback()
        session = SimpleNamespace(
            library_item_id="book-1",
            duration=300,
            current_time=42,
            tracks=[SimpleNamespace(start_offset=0, duration=300, url="http://example.test/book.mp3")],
        )

        with patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=FakeGst()):
            with patch("abs_kids_player.playback.preferred_bluetooth_alsa_device") as bluetooth_device:
                with patch("abs_kids_player.playback.time.sleep"):
                    playback.start(session, FakeClient())

        bluetooth_device.assert_not_called()
        self.assertEqual(playback.audio_output_device, "")
        self.assertIsNone(playback.playbin.properties["audio-sink"])

    def test_set_volume_state_applies_gstreamer_volume(self) -> None:
        playback = GStreamerPlayback()
        playback.playbin = FakePlaybin()

        playback.set_volume_state(75, muted=False)

        self.assertEqual(playback.volume_percent, 75)
        self.assertFalse(playback.muted)
        self.assertAlmostEqual(playback.playbin.properties["volume"], 0.75)
        self.assertNotIn("mute", playback.playbin.properties)

    def test_spoken_navigation_temporarily_ducks_and_restores_playback(self) -> None:
        playback = GStreamerPlayback()
        playback.playbin = FakePlaybin()
        playback.volume_percent = 75

        with patch("abs_kids_player.playback.time.sleep"):
            playback.set_spoken_navigation_ducked(True)
            ducked_volume = playback.playbin.properties["volume"]
            playback.set_spoken_navigation_ducked(False)

        self.assertAlmostEqual(ducked_volume, 0.75 * SPOKEN_NAVIGATION_DUCK_FACTOR)
        self.assertAlmostEqual(playback.playbin.properties["volume"], 0.75)

    def test_muted_volume_sets_effective_gstreamer_volume_to_zero(self) -> None:
        playback = GStreamerPlayback()
        playback.playbin = FakePlaybin()

        playback.set_volume_state(75, muted=True)

        self.assertEqual(playback.volume_percent, 75)
        self.assertTrue(playback.muted)
        self.assertEqual(playback.playbin.properties["volume"], 0.0)
        self.assertNotIn("mute", playback.playbin.properties)

    def test_zero_volume_counts_as_muted(self) -> None:
        playback = GStreamerPlayback()
        playback.playbin = FakePlaybin()

        playback.set_volume_state(0, muted=False)

        self.assertTrue(playback.muted)
        self.assertEqual(playback.playbin.properties["volume"], 0.0)

    def test_missing_mute_property_does_not_block_volume_control(self) -> None:
        playback = GStreamerPlayback()
        playback.playbin = FakePlaybin(reject_mute=True)

        playback.set_volume_state(40, muted=False)

        self.assertAlmostEqual(playback.playbin.properties["volume"], 0.4)
        self.assertNotIn("mute", playback.playbin.properties)

    def test_mute_pause_can_skip_progress_sync(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        playback.client = FakeClient()
        playback.session = SimpleNamespace(library_item_id="book-1", duration=300)
        playback.current_time = 42
        playback.is_playing = True
        playback.playbin.position = 42

        with patch("abs_kids_player.playback.time.sleep"):
            playback.pause(sync=False)

        self.assertFalse(playback.is_playing)
        self.assertTrue(playback.soft_paused)
        self.assertEqual(playback.soft_pause_time, 42)
        self.assertEqual(playback.playbin.properties["volume"], 0.0)
        self.assertEqual(playback.playbin.states, [])
        self.assertEqual(playback.client.calls, [])

    def test_mute_resume_can_skip_progress_sync(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        playback.client = FakeClient()
        playback.session = SimpleNamespace(
            library_item_id="book-1",
            duration=300,
            tracks=[SimpleNamespace(start_offset=0, duration=300)],
        )
        playback.current_time = 42

        with patch("abs_kids_player.playback.time.sleep"):
            playback.resume(sync=False)

        self.assertTrue(playback.is_playing)
        self.assertEqual(playback.playbin.states[-1], FakeGst.State.PLAYING)
        self.assertEqual(playback.client.calls, [])

    def test_smart_resume_rewind_depends_on_pause_duration(self) -> None:
        self.assertEqual(smart_resume_rewind_seconds(9.9), 0)
        self.assertEqual(smart_resume_rewind_seconds(10), 3)
        self.assertEqual(smart_resume_rewind_seconds(299), 3)
        self.assertEqual(smart_resume_rewind_seconds(300), 8)

    def test_soft_pause_resume_seeks_back_after_long_pause(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        playback.client = FakeClient()
        playback.session = SimpleNamespace(
            library_item_id="book-1",
            duration=300,
            tracks=[SimpleNamespace(start_offset=0, duration=300)],
        )
        playback.current_time = 42
        playback.volume_percent = 50
        playback.playbin.position = 42

        with patch("abs_kids_player.playback.time.sleep"):
            with patch("abs_kids_player.playback.time.monotonic", side_effect=[100.0, 112.0]):
                playback.pause(sync=False)
                playback.current_time = 100
                playback.resume(sync=False)

        self.assertTrue(playback.is_playing)
        self.assertFalse(playback.soft_paused)
        self.assertEqual(playback.current_time, 39)
        self.assertEqual(playback.playbin.seeks[-1], (FakeGst.Format.TIME, 3, 39))
        self.assertAlmostEqual(playback.playbin.properties["volume"], 0.5)

    def test_soft_pause_resume_does_not_rewind_after_short_pause(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        playback.client = FakeClient()
        playback.session = SimpleNamespace(
            library_item_id="book-1",
            duration=300,
            tracks=[SimpleNamespace(start_offset=0, duration=300)],
        )
        playback.current_time = 42

        with patch("abs_kids_player.playback.time.sleep"):
            with patch("abs_kids_player.playback.time.monotonic", side_effect=[100.0, 105.0]):
                playback.pause(sync=False)
                playback.current_time = 100
                playback.resume(sync=False)

        self.assertEqual(playback.current_time, 42)
        self.assertEqual(playback.playbin.seeks[-1], (FakeGst.Format.TIME, 3, 42))

    def test_soft_pause_resume_owns_fade_up_to_new_volume(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        playback.client = FakeClient()
        playback.session = SimpleNamespace(
            library_item_id="book-1",
            duration=300,
            tracks=[SimpleNamespace(start_offset=0, duration=300)],
        )
        playback.current_time = 42
        playback.volume_percent = 0
        playback.muted = True
        playback.soft_paused = True
        playback.soft_pause_time = 42
        playback._effective_volume = SILENCE_VOLUME

        with patch("abs_kids_player.playback.time.sleep"):
            playback.resume(sync=False, percent=50, muted=False)

        self.assertEqual(playback.playbin.volume_values[0], PREROLL_VOLUME_FLOOR)
        self.assertEqual(playback.playbin.states[-1], FakeGst.State.PLAYING)
        self.assertAlmostEqual(playback.playbin.volume_values[-1], 0.5)
        self.assertFalse(playback.muted)

    def test_soft_pause_fades_volume_down_to_true_silence(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        playback.client = FakeClient()
        playback.session = SimpleNamespace(library_item_id="book-1", duration=300)
        playback.current_time = 42
        playback.volume_percent = 50
        playback._effective_volume = 0.5

        with patch("abs_kids_player.playback.time.sleep"):
            playback.pause(sync=False)

        self.assertGreater(len(playback.playbin.volume_values), 1)
        self.assertEqual(playback.playbin.volume_values[-1], 0.0)
        self.assertGreater(playback.playbin.volume_values[0], playback.playbin.volume_values[-1])

    def test_poll_does_not_query_position_while_paused(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        playback.is_playing = False

        playback.poll()

        self.assertEqual(playback.playbin.query_position_calls, 0)

    def test_poll_queues_current_audiobookshelf_position_after_playback_starts(self) -> None:
        session = PlaybackSession(
            "session-1", "book-1", "Book", "Author", 300, 12, "",
            [AudioTrack(1, 0, 300, "https://books.example.com/book.mp3", "Track")], [],
        )
        client = client_from_config(AppConfig(server_url="https://books.example.com", token="test-token"))
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {"ABS_KIDS_PLAYER_STATE_PATH": str(Path(directory) / "state.json")}))
        playback = GStreamerPlayback(progress_sync=ProgressSync())

        with (
            patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=FakeGst()),
            patch("abs_kids_player.playback.time.sleep"),
            patch("abs_kids_player.playback.time.monotonic", return_value=100.0),
        ):
            playback.start(session, client)
        playback.playbin.position = 42

        with (
            patch.object(playback, "handle_bus_messages"),
            patch("abs_kids_player.playback.time.monotonic", return_value=116.0),
        ):
            playback.poll()

        update = read_pending_progress()[(client.progress_account, "book-1", "")]
        self.assertEqual((update.current_time, update.duration, update.session_id), (42, 300, "session-1"))
        self.assertEqual(update.time_listened, 16)
        self.assertTrue(playback.is_playing)

    def test_downloaded_playback_saves_progress_periodically_and_on_stop_without_streaming(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict("os.environ", {
            "ABS_KIDS_PLAYER_STATE_PATH": str(directory / "state.json"),
            "ABS_KIDS_PLAYER_LIBRARY_CACHE_PATH": str(directory / "library-cache.json"),
        }))
        config = AppConfig(server_url="https://books.example.com", username="listener", token="test-token")
        client = client_from_config(config)
        folder = book_directory(config, "book")
        download = DownloadedBook(Book("book", "Book", "Author", 300, ""), [
            AudioTrack(1, 0, 300, "track-1", "Track", size_bytes=4),
        ], [], complete=True)
        download.save(folder)
        (folder / "track-1").write_bytes(b"test")
        # Even with the Pi clock behind the server, an explicit restart is a newer edit.
        self.enterContext(patch("abs_kids_player.progress_sync.time.time_ns", return_value=100_000_000))
        save_cached_books(config, [Book("book", "Book", "Author", 300, "", current_time=90, progress_updated_at=400)])
        sync = ProgressSync()
        sync.record(client.progress_account, "book", 80, 300, after=499)
        playback = GStreamerPlayback(progress_sync=sync)
        session = prepare_book_playback("book", config, local_only=True, start_over=True).session
        key = (client.progress_account, "book", "")

        with (
            patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=FakeGst()),
            patch("abs_kids_player.playback.time.sleep"),
            patch("abs_kids_player.playback.time.monotonic", return_value=100.0) as clock,
            patch.object(playback, "handle_bus_messages"),
            patch.object(client, "authenticated_media_url") as media_url,
        ):
            playback.start(session, client, start_over=True)
            self.assertEqual(playback.playbin.properties["uri"], (folder / "track-1").as_uri())
            restart = read_pending_progress()[key]
            self.assertEqual((restart.current_time, restart.updated_at), (0, 501))
            clock.return_value = 116
            playback.playbin.position = 16
            playback.poll()
            self.assertEqual(read_pending_progress()[key].current_time, 16)
            clock.return_value = 119
            playback.playbin.position = 19
            playback.poll()
            playback.stop()
            media_url.assert_not_called()

        self.assertEqual(read_pending_progress()[key].current_time, 19)

    def test_pausing_rss_playback_saves_the_selected_episode_position(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {"ABS_KIDS_PLAYER_STATE_PATH": str(Path(directory) / "state.json")}))
        playback = GStreamerPlayback(progress_sync=ProgressSync())
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        playback.client = PodcastProgressClient("listener", "episode-1")
        playback.session = PlaybackSession("session", "podcast:show", "Show", "", 300, 80, "", [], [])
        playback.current_time = 80
        playback.is_playing = True

        playback.pause(keep_audio_alive=False)

        key = podcast_progress_key("listener", "podcast:show", "episode-1")
        self.assertEqual(podcast_progress()[key]["currentTime"], 80)
        self.assertFalse(read_pending_progress())
        self.assertFalse(playback.is_playing)

    def test_poll_stops_after_end_of_session_clears_player(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playbin = FakePlaybin()
        playback.playbin = playbin
        playback.is_playing = True

        with patch.object(playback, "handle_bus_messages", side_effect=playback.stop):
            playback.poll()

        self.assertEqual(playbin.query_position_calls, 0)
        self.assertFalse(playback.is_playing)

    def test_multifile_audiobook_advances_only_within_active_session(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        session = SimpleNamespace(
            id="session-1",
            library_item_id="book-1",
            duration=200,
            tracks=[
                SimpleNamespace(start_offset=0, duration=100, url="http://example.test/part-1.mp3"),
                SimpleNamespace(start_offset=100, duration=100, url="http://example.test/part-2.mp3"),
            ],
        )
        playback.session = session
        playback.client = FakeClient()
        playback.is_playing = True

        with patch.object(playback, "load_current_track") as load_current_track:
            advanced = playback.advance_within_session()

        self.assertTrue(advanced)
        self.assertIs(playback.session, session)
        self.assertEqual(playback.session.library_item_id, "book-1")
        self.assertEqual(playback.track_index, 1)
        self.assertEqual(playback.current_time, 100)
        self.assertIsNone(playback.completed_session)
        load_current_track.assert_called_once_with()

    def test_completed_audiobook_stops_instead_of_starting_another_book(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        session = SimpleNamespace(
            id="session-1",
            library_item_id="book-1",
            duration=100,
            tracks=[SimpleNamespace(start_offset=0, duration=100, url="http://example.test/book.mp3")],
        )
        playback.session = session
        playback.client = FakeClient()
        playback.is_playing = True

        with patch.object(playback, "request_progress_sync") as sync_progress:
            advanced = playback.advance_within_session()

        self.assertFalse(advanced)
        sync_progress.assert_called_once_with()
        self.assertIsNone(playback.session)
        self.assertIsNone(playback.playbin)
        self.assertIs(playback.completed_session, session)
        playback.stop()
        self.assertIsNone(playback.completed_session)
        self.assertFalse(playback.is_playing)

    def test_completed_podcast_stops_instead_of_starting_another_title(self) -> None:
        playback = GStreamerPlayback()
        playback.Gst = FakeGst()
        playback.playbin = FakePlaybin()
        playback.session = SimpleNamespace(
            id="podcast:yoto-daily",
            library_item_id="podcast:yoto-daily",
            duration=600,
            tracks=[SimpleNamespace(start_offset=0, duration=600, url="http://example.test/episode.mp3")],
        )
        playback.client = FakeClient()
        playback.is_playing = True

        with patch.object(playback, "request_progress_sync"):
            advanced = playback.advance_within_session()

        self.assertFalse(advanced)
        self.assertIsNone(playback.session)
        self.assertIsNone(playback.playbin)
        self.assertFalse(playback.is_playing)

    def test_sync_progress_passes_session_id_and_time_listened(self) -> None:
        playback = GStreamerPlayback()
        client = FakeClient()
        playback.client = client
        playback.session = SimpleNamespace(id="play-1", library_item_id="book-1", duration=300)
        playback.current_time = 42
        playback.is_playing = True

        with patch("abs_kids_player.playback.time.monotonic", return_value=112.0):
            playback.last_sync = 100.0
            playback.sync_progress()

        self.assertEqual(client.calls[0][:4], ("book-1", 42, 300, "play-1"))
        self.assertEqual(client.calls[0][4], 12.0)


class ResumeSeekTest(unittest.TestCase):
    def setUp(self) -> None:
        self.player = GStreamerPlayback()
        self.playbin = FakePlaybin()
        self.gst = FakeGst()
        self.gst.SECOND = 1000
        self.now = 10.0
        self.timing = PlaybackTiming(9.0, 9.1, "continue", clock=lambda: self.now)
        self.query = SimpleNamespace(parse_seeking=lambda: (self.gst.Format.TIME, True, 0, -1))
        self.gst.Query = SimpleNamespace(new_seeking=Mock(return_value=self.query))
        self.playbin.get_property = Mock(side_effect={"n-audio": 1}.__getitem__)
        self.playbin.query = Mock(return_value=True)
        self.session = SimpleNamespace(
            duration=460,
            current_time=342,
            tracks=[
                SimpleNamespace(start_offset=0, duration=300, url="http://example.test/one.m4b"),
                SimpleNamespace(start_offset=300, duration=160, url="http://example.test/two.m4b"),
            ],
        )

    def start(self, results, *, cancelled=None) -> None:
        states = iter(results)
        get_state = self.playbin.get_state

        def next_state(timeout: int):
            get_state(timeout)
            status = next(states)
            if status == FakeGst.StateChangeReturn.ASYNC:
                self.now += timeout / self.gst.SECOND
            return status, FakeGst.State.PAUSED, None

        with (
            patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=self.gst),
            patch.object(
                self.gst.ElementFactory, "make", side_effect=[self.playbin, FakePlaybin()]
            ),
            patch.object(self.playbin, "get_state", side_effect=next_state),
            patch("abs_kids_player.playback.time.sleep"),
        ):
            self.player.start(self.session, FakeClient(), timing=self.timing, cancelled=cancelled)

    def test_seeks_to_track_offset_during_preroll_and_waits_for_target_once(self) -> None:
        def audio_streams(_name):
            return int(self.now > 10.0)

        self.playbin.get_property.side_effect = audio_streams
        states = self.gst.StateChangeReturn
        self.start([states.ASYNC, states.ASYNC, states.SUCCESS])

        self.playbin.query.assert_called_once_with(self.query)
        self.assertEqual(self.playbin.seeks, [(FakeGst.Format.TIME, 3, 42000)])
        self.assertEqual(self.player.track_index, 1)
        self.assertEqual(self.timing.preroll_wait, "seek_started")
        self.assertEqual(self.timing.seek_mode, "early")
        self.assertAlmostEqual(self.timing.seek_at, 10.025)
        self.assertEqual(self.timing.preroll_finished_at, self.timing.seek_at)
        self.assertEqual(self.timing.seek_wait, "completed")
        self.assertLess(
            self.playbin.events.index(("seek", 42000)),
            self.playbin.events.index(("get_state", 5 * self.gst.SECOND)),
        )
        seek_wait_index = self.playbin.events.index(("get_state", 5 * self.gst.SECOND))
        unmute_index = next(
            index
            for index, (event, value) in enumerate(self.playbin.events)
            if event == "volume" and value > PREROLL_VOLUME_FLOOR
        )
        self.assertLess(seek_wait_index, unmute_index)

    def test_rejected_early_seek_falls_back_after_preroll_at_the_configured_rate(self) -> None:
        self.player.set_playback_rate(1.5)
        self.playbin.seek = Mock(side_effect=[False, True])
        states = self.gst.StateChangeReturn
        self.start([states.ASYNC, states.ASYNC, states.SUCCESS, states.SUCCESS])

        expected_seek = (
            1.5,
            self.gst.Format.TIME,
            3,
            self.gst.SeekType.SET,
            42000,
            self.gst.SeekType.NONE,
            -1,
        )
        self.assertEqual(
            self.playbin.seek.call_args_list, [call(*expected_seek), call(*expected_seek)]
        )
        self.playbin.query.assert_called_once_with(self.query)
        self.assertEqual(self.timing.seek_mode, "after_preroll")
        self.assertEqual(self.timing.preroll_wait, "completed")
        self.assertEqual(self.timing.seek_wait, "completed")
        self.assertLess(self.timing.early_seek_attempt_at, self.timing.seek_at)

    def test_cancelled_during_seekability_query_never_seeks_or_plays(self) -> None:
        cancelled = False

        def query_and_cancel(_query):
            nonlocal cancelled
            cancelled = True
            return True

        self.playbin.query.side_effect = query_and_cancel
        self.start([self.gst.StateChangeReturn.ASYNC], cancelled=lambda: cancelled)

        self.assertEqual(self.playbin.seeks, [])
        self.assertEqual(self.timing.preroll_wait, "cancelled")
        self.assertNotIn(self.gst.State.PLAYING, self.playbin.states)
        self.assertFalse(self.player.is_playing)

    def test_continue_logs_the_selected_track_and_captures_http_through_the_seek(self) -> None:
        self.session = PlaybackSession(
            id="play-1",
            library_item_id="book-1",
            title="Book",
            author="Author",
            duration=460,
            current_time=342,
            cover_url="",
            tracks=[
                AudioTrack(1, 0, 300, "https://example.test/one.m4b", "One", file_id=701),
                AudioTrack(
                    2,
                    300,
                    160,
                    "https://example.test/two.m4b?token=secret",
                    "Two",
                    file_id=702,
                    size_bytes=1234567,
                ),
            ],
            chapters=[],
            play_method=0,
        )
        self.timing.source = "audiobookshelf"
        self.player.set_volume_state(0, True)
        bus = Mock()
        bus.connect.return_value = 7
        self.playbin.get_bus = Mock(return_value=bus)
        set_state = self.playbin.set_state
        seek_simple = self.playbin.seek_simple

        def post_http_status(status):
            headers = Mock()
            headers.get_name.return_value = "http-headers"
            headers.get_uint.return_value = (True, status)
            headers.get_value.return_value = None
            observe = bus.connect.call_args.args[1]
            observe(bus, SimpleNamespace(get_structure=lambda: headers))

        def on_state(state):
            set_state(state)
            if state == self.gst.State.PAUSED:
                self.now += 0.1
                post_http_status(200)
                self.now += 0.1

        def on_seek(*args):
            self.now += 0.15
            post_http_status(206)
            self.now += 0.05
            return seek_simple(*args)

        states = self.gst.StateChangeReturn
        with (
            patch.dict("os.environ", {"CHAPTER_TRACE_HTTP_RANGES": "1"}),
            patch.object(self.playbin, "set_state", side_effect=on_state),
            patch.object(self.playbin, "seek_simple", side_effect=on_seek),
        ):
            self.start([states.SUCCESS, states.SUCCESS])
        self.timing.startup_finished_at = self.now
        with patch("builtins.print") as printer:
            self.timing.report()

        timing_line, first_response, second_response = [
            entry.args[0] for entry in printer.call_args_list
        ]
        for field in (
            "track_number=2",
            "track_count=2",
            "track_file_id=702",
            "track_size_bytes=1234567",
            "book_resume_s=342.000",
            "track_start_s=300.000",
            "track_seek_s=42.000",
            "abs_play_method=0",
            "gst_http_responses=2",
        ):
            self.assertIn(field, timing_line.split())
        self.assertNotIn("secret", timing_line)
        self.assertIn("phase=preroll status=200", first_response)
        self.assertIn("phase=seek status=206", second_response)
        bus.disconnect.assert_called_once_with(7)
        bus.disable_sync_message_emission.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
