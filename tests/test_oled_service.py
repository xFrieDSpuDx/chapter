from __future__ import annotations

import queue
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from datetime import datetime
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import Mock, patch

from abs_kids_player import oled_catalogue, oled_service
from abs_kids_player.api import AudiobookshelfError
from abs_kids_player.appliance_io import InputEvent
from abs_kids_player.audio import VolumeState
from abs_kids_player.bluetooth_audio import BluetoothActionResult
from abs_kids_player.bluetooth_picker import SpeakerPicker
from abs_kids_player.catalogue_settings import (
    CATALOGUE_COMMAND,
    LibraryOption,
    catalogue_request_key,
)
from abs_kids_player.config import (
    LIBRARY_SORT_AUTHOR,
    AppConfig,
    PodcastConfig,
)
from abs_kids_player.display_frames import (
    SCREEN_OFF,
    HomeFrame,
    PlayingFrame,
    ScreenSaverFrame,
    SleepConfirmFrame,
    SplashFrame,
    StartupResumeFrame,
    TwoLineFrame,
    VolumeFrame,
    WifiSetupFrame,
)
from abs_kids_player.listening_timer import ListeningSleepTimer
from abs_kids_player.models import AudioTrack, Book, Chapter, PlaybackSession
from abs_kids_player.oled_bluetooth import BluetoothStatusResult, OledBluetooth
from abs_kids_player.oled_catalogue import BookLoadResult, book_load_result, start_book_load_worker
from abs_kids_player.oled_input import OledInputQueue
from abs_kids_player.oled_service import (
    BLUETOOTH_POWER_FRAME,
    BOOK_LOADING_FRAME,
    SOFTWARE_UPDATE_FRAME,
    apply_sleep_timer_event,
    bluetooth_result_frame,
    frame_for_boot_state,
    frame_for_wifi_status,
    frame_with_mute_indicator,
    frame_with_status_indicators,
    handle_menu_command,
    handle_web_player_commands,
    home_frame_for_books,
    load_home_frame,
    poll_playback_safely,
    refresh_home_cache,
    save_playback_state,
    scrolled_home_frame,
    set_amp_shutdown,
    should_return_to_active_session,
    signal_boot_splash_handoff,
    splash_frame,
    unmute_for_audible_playback_commands,
    update_display_brightness,
    web_status_for_playback,
)
from abs_kids_player.oled_web_status import WebStatusPublisher
from abs_kids_player.playback import GStreamerPlayback, PlaybackError
from abs_kids_player.playback_owner import PlaybackOwner, PlaybackSnapshot, PlaybackStartResult
from abs_kids_player.player_control import (
    WebPlayerCommand,
    WebPlayerStatus,
    load_web_player_status,
    queue_web_player_command,
)
from abs_kids_player.player_state import LastPlaybackState, load_player_state, save_last_playback
from abs_kids_player.podcast import podcast_book
from abs_kids_player.rotary_ui import ApplianceMenu, CommandType, MenuCommand, Screen
from abs_kids_player.software_update import SoftwareUpdateState
from abs_kids_player.volume_controls import DEFAULT_UNMUTE_VOLUME_PERCENT
from abs_kids_player.wifi import SETUP_HOTSPOT_SSID, SETUP_HOTSPOT_URL, WifiStatus


class FakeClient:
    def __init__(self, _server_url: str, _token: str) -> None:
        pass

    def choose_library_id(self, configured_id: str = "") -> str:
        return configured_id or "library-1"

    def get_books(self, _library_id: str) -> list[Book]:
        return [Book(id="book-1", title="The Hobbit", author="J.R.R. Tolkien", duration=1, cover_url="")]


class FakeFailingClient:
    def __init__(self, _server_url: str, _token: str) -> None:
        pass

    def choose_library_id(self, _configured_id: str = "") -> str:
        raise AudiobookshelfError("server unavailable")


def make_podcast_config() -> PodcastConfig:
    return PodcastConfig(
        url="https://podcasts.apple.com/us/podcast/story-show/id123",
        title="Story Show",
        author="Story Parent",
        feed_url="https://feeds.example.com/story.xml",
        book_id="podcast:apple:123",
    )


class FakeAmpShutdown:
    def __init__(self) -> None:
        self.states: list[bool] = []

    def set_shutdown(self, shutdown: bool) -> None:
        self.states.append(shutdown)


class FakePlayback:
    opening_key = ""
    starting_book = False
    audio_output_device = ""
    volume_output = "speaker"
    output_volumes = None

    def __init__(self, session: PlaybackSession | None = None) -> None:
        self.session = session
        self.current_time = 0.0
        self.is_playing = False
        self.seek_times: list[float] = []
        self.resume_calls = 0
        self.pause_calls = 0
        self.stop_calls = 0
        self.audio_refresh_calls = 0
        self.volume_states: list[tuple[int, bool]] = []

    @property
    def state(self) -> PlaybackSnapshot:
        return PlaybackSnapshot(self.session, self.current_time, self.is_playing)

    def cancel_book_request(self) -> None:
        pass

    def seek_global(self, seek_time: float) -> None:
        self.current_time = seek_time
        self.seek_times.append(seek_time)

    def resume(self, sync: bool = False, percent: int | None = None, muted: bool | None = None) -> bool:
        if self.session is None or self.is_playing:
            return False
        self.is_playing = True
        self.resume_calls += 1
        if percent is not None and muted is not None:
            self.volume_states.append((percent, muted))
        return True

    def pause(self, sync: bool = True) -> bool:
        if not self.is_playing:
            return False
        self.is_playing = False
        self.pause_calls += 1
        return True

    def stop(self) -> None:
        self.session = None
        self.current_time = 0.0
        self.is_playing = False
        self.stop_calls += 1

    def refresh_audio_output(self) -> None:
        self.audio_refresh_calls += 1

    def set_volume_state(self, percent: int, muted: bool) -> None:
        self.volume_states.append((percent, muted))


class FakePollingErrorPlayback:
    def poll(self) -> None:
        raise PlaybackError("GStreamer playback error.")


class FakeStartingPlayback(FakePlayback):
    progress_sync = None
    def __init__(self) -> None:
        super().__init__()
        self.started_sessions: list[PlaybackSession] = []
        self.start_over_flags: list[bool] = []

    def start(self, session, _client, *, start_over, cancelled, timing=None) -> None:
        self.session = session
        self.current_time = session.current_time
        self.is_playing = True
        self.started_sessions.append(session)
        self.start_over_flags.append(start_over)
        if timing is not None:
            timing.observe_output("output_observed", rendered_at=timing.clock())


def wait_for_playback_start(playback: PlaybackOwner) -> PlaybackStartResult:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = playback.advance_book_request()
        if result is not None:
            return result
        time.sleep(0.001)
    raise AssertionError("Playback start did not finish")


class FakePlaybackClient:
    def __init__(self, _server_url: str, _token: str) -> None:
        pass

    def start_playback(self, _book_id: str, start_over: bool = False, resume_time: float = 0) -> PlaybackSession:
        session = make_playback_session()
        session.current_time = 0 if start_over else resume_time
        return session


def make_playback_session() -> PlaybackSession:
    return PlaybackSession(
        id="session-1",
        library_item_id="book-1",
        title="The Hobbit",
        author="J.R.R. Tolkien",
        duration=300,
        current_time=60,
        cover_url="",
        tracks=[AudioTrack(1, 0, 300, "http://example.test/book.mp3", "Track")],
        chapters=[
            Chapter(1, "An Unexpected Party", 0, 120),
            Chapter(2, "Roast Mutton", 120, 300),
        ],
    )


class OledServiceTest(unittest.TestCase):
    def test_physical_turn_is_reported_after_the_volume_frame_is_written(self):
        events, display = OledInputQueue(), Mock()
        events.put(InputEvent("volume", 1))
        display.show.return_value = True
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "capture"
            marker.touch()
            with (patch("abs_kids_player.dial_timing.CAPTURE_MARKER", marker),
                  patch("builtins.print") as messages):
                self.run_service(display, events, AppConfig(),
                                 lambda shutdown, seconds: shutdown.request(0, None) if seconds else None,
                                 wifi_status=lambda: WifiStatus(False), load_saved_volume=lambda: VolumeState(3))
        self.assertTrue(any(str(call.args[0]).startswith("Dial timing dial=volume turns=1 frame=VolumeFrame ")
                            for call in messages.call_args_list if call.args))
        display.show.assert_any_call(VolumeFrame(4))

    def test_volume_turn_draws_while_the_periodic_shelf_scan_is_blocked(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def slow_scan(config):
            started.set()
            release.wait(3)
            finished.set()
            return []

        display, events = Mock(), OledInputQueue()
        events.put(InputEvent("volume", 1))
        observed = []
        def after_frame(shutdown, seconds):
            if seconds > 0:
                observed.append((display.show.call_args.args[0], started.wait(1), finished.is_set()))
                release.set()
                shutdown.request(0, None)

        with (patch.object(oled_service, "DEFAULT_RUNTIME_CONFIG_REFRESH_SECONDS", 0),
              patch("abs_kids_player.oled_shelf.account_downloads", slow_scan)):
            self.run_service(display, events, AppConfig(), after_frame,
                             wifi_status=lambda: WifiStatus(False), load_saved_volume=lambda: VolumeState(3))
        self.assertEqual(observed, [(VolumeFrame(4), True, False)])
        self.assertTrue(finished.is_set())

    def test_streaming_resume_prompt_survives_waiting_for_the_first_wifi_check(self):
        display = Mock()
        frames = []
        downloads = [Book("local", "Downloaded story", "", 60, "", downloaded=True)]
        network = Mock()
        network.current.side_effect = [None, WifiStatus(True, ssid="home")]

        def after_frame(shutdown, seconds):
            if seconds > 0:
                frames.append(display.show.call_args.args[0])
                if len(frames) == 2:
                    shutdown.request(0, None)

        self.run_service(display, OledInputQueue(), AppConfig(), after_frame,
                         DeviceStatusCache=Mock(return_value=network),
                         books_with_downloads=Mock(return_value=downloads),
                         load_player_state=Mock(return_value=LastPlaybackState("stream", "Streamed story", current_time=30)))
        self.assertTrue(all(isinstance(frame, StartupResumeFrame) and frame.title == "Streamed story" for frame in frames))

    def test_volume_dial_updates_the_screen_while_networkmanager_is_still_busy(self):
        started, release, completed = threading.Event(), threading.Event(), threading.Event()
        def busy_probe():
            started.set()
            release.wait(timeout=3)
            completed.set()
            return WifiStatus(connected=True, ssid="home")

        display, events = Mock(), OledInputQueue()
        events.put(InputEvent("volume", 1))
        frames = []
        def after_frame(shutdown, seconds):
            if seconds > 0:
                frames.append((display.show.call_args.args[0], started.wait(timeout=1), completed.is_set()))
                shutdown.request(0, None)
        try:
            self.run_service(display, events, AppConfig(), after_frame, DeviceStatusCache=oled_service.DeviceStatusCache,
                             wifi_status=busy_probe, load_saved_volume=Mock(return_value=VolumeState(3)))
        finally:
            release.set()
        self.assertEqual(frames, [(VolumeFrame(4), True, False)])
        self.assertTrue(completed.wait(timeout=1))

    def test_catalogue_refresh_keeps_the_selection_when_it_succeeds_or_fails(self) -> None:
        config = AppConfig(server_url="https://books.example.test", token="token", library_id="first")
        selected = Book("selected", "Selected", "", 60, "")
        books = [Book("before", "Before", "", 60, ""), selected]
        command = WebPlayerCommand(CATALOGUE_COMMAND, id="refresh", catalogue_key=catalogue_request_key(config))
        for successful in (True, False):
            with self.subTest(successful=successful):
                display, events = Mock(), OledInputQueue()
                events.put(InputEvent("nav", steps=1))
                receipts, report, cache = Mock(), Mock(), Mock()

                def start(_config, results, *, generation, library_id="") -> None:
                    if successful:
                        results.put(BookLoadResult(
                            books=[Book("new", "A new book", "", 60, ""), *books], generation=generation
                        ))
                    else:
                        results.put(BookLoadResult(
                            error_frame=TwoLineFrame("Server unreachable", "Press to retry"), generation=generation
                        ))

                def finish(shutdown, seconds) -> None:
                    if seconds > 0:
                        shutdown.request(0, None)

                self.run_service(
                    display, events, config, finish,
                    load_cached_books=Mock(return_value=books),
                    wifi_status=Mock(return_value=WifiStatus(connected=successful, ssid="home" if successful else "")),
                    consume_web_player_commands=Mock(return_value=[command]),
                    start_book_load_worker=Mock(side_effect=start),
                    save_cached_books=cache, report_catalogue_result=report, save_command_result=receipts,
                )

                self.assertEqual(display.show.call_args.args[0].title, "Selected")
                receipts.assert_called_once_with(command, "applied" if successful else "rejected")
                if successful:
                    self.assertEqual(cache.call_args.args[1][0].id, "new")
                    report.assert_called_once_with(config)
                else:
                    cache.assert_not_called()
                    report.assert_called_once_with(config, "Server unreachable")

    def test_newer_library_selection_prevents_an_older_result_from_saving(self) -> None:
        config = AppConfig(server_url="https://books.example.test", token="token", library_id="first")
        key = catalogue_request_key(config)
        earlier = WebPlayerCommand(CATALOGUE_COMMAND, id="earlier", catalogue_key=key, library_id="earlier")
        latest = WebPlayerCommand(CATALOGUE_COMMAND, id="latest", catalogue_key=key, library_id="latest")
        display, events = Mock(), OledInputQueue()
        def transaction(change):
            return config, change(config)

        saved, cache, receipts = Mock(side_effect=transaction), Mock(), Mock()
        generations = {}

        def start(_config, results, *, generation, library_id="") -> None:
            generations[library_id] = generation
            if library_id == "latest":
                for target in ("earlier", "latest"):
                    results.put(BookLoadResult(
                        books=[Book(target, target.title(), "", 60, "")],
                        generation=generations[target], library=LibraryOption(target, target.title()),
                    ))

        def finish(shutdown, seconds) -> None:
            if seconds > 0 and (receipts.call_count >= 2 or display.show.call_count >= 6):
                shutdown.request(0, None)

        self.run_service(
            display, events, config, finish,
            consume_web_player_commands=Mock(side_effect=[[earlier], [latest], [], [], []]),
            start_book_load_worker=Mock(side_effect=start),
            update_config=saved, save_cached_books=cache, save_command_result=receipts,
            wifi_status=Mock(return_value=WifiStatus(connected=True, ssid="home")),
        )

        self.assertEqual(saved.call_count, 1)
        self.assertEqual(cache.call_args.args[0].library_id, "latest")
        self.assertEqual(cache.call_args.args[1][0].id, "latest")
        self.assertEqual([call.args for call in receipts.call_args_list],
                         [(earlier, "superseded"), (latest, "applied")])

    def test_startup_retries_unreadable_settings_after_showing_recovery_message(self):
        from abs_kids_player.config import ConfigReadError

        config = AppConfig()
        display = Mock()
        loader = Mock(side_effect=[ConfigReadError("settings"), config] + [config] * 10)

        def finish(shutdown, seconds):
            if loader.call_count > 1:
                shutdown.request(0, None)

        self.run_service(display, OledInputQueue(), config, finish, load_config=loader)
        display.show.assert_any_call(TwoLineFrame("Settings unreadable", "Check config.json"))
        self.assertGreaterEqual(loader.call_count, 2)

    def test_navigation_click_retries_a_failed_library_load(self) -> None:
        display = Mock()
        events = OledInputQueue()
        loads = [
            BookLoadResult(error_frame=TwoLineFrame("Server unreachable", "Press to retry")),
            BookLoadResult(books=[Book("book-1", "The Hobbit", "J.R.R. Tolkien", 1, "")]),
        ]
        config = AppConfig(server_url="https://books.example.com", token="token", library_id="library-1")

        def run_book_load(_config, results, *, generation, **_options) -> None:
            result = loads.pop(0)
            result.generation = generation
            results.put(result)

        def after_frame(shutdown, seconds) -> None:
            if seconds <= 0:
                return
            frame = display.show.call_args.args[0]
            if frame == TwoLineFrame("Server unreachable", "Press to retry"):
                events.put(InputEvent("nav_click"))
            if isinstance(frame, HomeFrame) or display.show.call_count >= 8:
                shutdown.request(0, None)

        self.run_service(
            display,
            events,
            config,
            after_frame,
            start_book_load_worker=Mock(side_effect=run_book_load),
            wifi_status=Mock(return_value=WifiStatus(connected=True, ssid="home")),
        )

        display.show.assert_any_call(TwoLineFrame("Server unreachable", "Press to retry"))
        self.assertEqual(display.show.call_args.args[0].title, "The Hobbit")
        self.assertFalse(loads)

    def test_failed_podcast_lookup_can_be_reopened_with_the_knob(self):
        display, events = Mock(), OledInputQueue()
        failed, recovered = Future(), Future()
        failed.set_exception(IncompleteRead(b""))
        recovered.set_result([Book("show/episode", "Episode", "", 100, "", episode_id="episode",
                                   source_item_id="show", podcast_title="Test show")])
        request = Mock(side_effect=[failed, recovered])
        for event in (InputEvent("nav", -1), InputEvent("nav_click"), InputEvent("nav", 3), InputEvent("nav_click")):
            events.put(event)
        frames = []
        def after_draw(shutdown, seconds):
            if seconds <= 0:
                return
            frame = display.show.call_args.args[0]
            frames.append(frame)
            if frame.top == "Podcasts unavailable":
                events.put(InputEvent("nav_click"))  # Back from the empty list
                events.put(InputEvent("nav_click"))  # Reopen Podcasts
            if frame.bottom == "Test show" or len(frames) >= 5:
                shutdown.request(0, None)

        self.run_service(display, events, AppConfig(), after_draw, request_podcast_catalogue=request,
                         load_cached_books=Mock(return_value=[Book("book", "A book", "", 100, "")]),
                         wifi_status=Mock(return_value=WifiStatus(connected=True, ssid="home")))
        self.assertEqual(frames, [TwoLineFrame("Podcasts unavailable", "Open Podcasts to retry"),
                                  TwoLineFrame("Podcasts", "Test show")])
        self.assertEqual(request.call_count, 2)

    def test_web_state_storage_failure_leaves_the_next_volume_turn_usable(self):
        display, events = Mock(), OledInputQueue()
        events.put(InputEvent("volume", 1))
        writes = Mock(side_effect=OSError("state storage unavailable"))
        commands = Mock(side_effect=OSError("command storage unavailable"))
        frames = []
        def after_draw(shutdown, seconds):
            if seconds > 0:
                frames.append(display.show.call_args.args[0])
                if len(frames) == 1:
                    events.put(InputEvent("volume", 1))
                else:
                    shutdown.request(0, None)

        self.run_service(display, events, AppConfig(), after_draw, save_web_player_status=writes,
                         consume_web_player_commands=commands, load_saved_volume=Mock(return_value=VolumeState(3)),
                         wifi_status=Mock(return_value=WifiStatus(connected=False)))
        self.assertEqual(frames, [VolumeFrame(percent=4), VolumeFrame(percent=5)])
        self.assertEqual(writes.call_count, 1)
        commands.assert_called_once_with()

    def test_status_publication_recovers_with_the_latest_volume_and_display_timestamp(self):
        playback = FakePlayback(make_playback_session())
        write = Mock(side_effect=[PermissionError("status unavailable"), None])
        publisher = WebStatusPublisher(write)
        with patch("abs_kids_player.oled_web_status.time.monotonic", side_effect=[10, 11, 15]):
            publisher.publish(playback, 3, False)
            publisher.publish(playback, 4, False)
            publisher.publish(playback, 5, False, display_drawn=True)
        first, latest = [call.args[0] for call in write.call_args_list]
        self.assertEqual((first.volume_percent, latest.volume_percent), (3, 5))
        self.assertGreater(latest.display_drawn_at, 0)

    def test_quiet_volume_is_restored_and_mute_works_with_spoken_navigation_disabled(self) -> None:
        display = Mock()
        events = OledInputQueue()
        events.put(InputEvent("volume_click"))
        saved_status = Mock()

        def stop_after_input(shutdown, seconds) -> None:
            if seconds > 0:
                shutdown.request(0, None)

        self.run_service(
            display,
            events,
            AppConfig(spoken_navigation_enabled=False),
            stop_after_input,
            load_web_player_status=Mock(return_value=WebPlayerStatus(volume_percent=3)),
            save_web_player_status=saved_status,
            wifi_status=Mock(return_value=WifiStatus(connected=False)),
        )

        display.show.assert_any_call(VolumeFrame(percent=3, muted=True))
        self.assertEqual(saved_status.call_args.args[0].volume_percent, 3)
        self.assertTrue(saved_status.call_args.args[0].muted)

    def test_playback_is_polled_during_a_volume_overlay_without_wifi(self) -> None:
        display = Mock()
        events = OledInputQueue()
        events.put(InputEvent("volume", steps=1))
        poll = Mock(return_value=None)

        def stop_after_frame(shutdown, seconds) -> None:
            if seconds > 0:
                self.assertIsInstance(display.show.call_args.args[0], VolumeFrame)
                poll.assert_called_once()
                shutdown.request(0, None)

        self.run_service(
            display,
            events,
            AppConfig(spoken_navigation_enabled=False),
            stop_after_frame,
            poll_playback_safely=poll,
            wifi_status=Mock(return_value=WifiStatus(connected=False)),
        )

    def test_service_starts_a_book_and_opens_more_without_squeezing_the_player_controls(self) -> None:
        display = Mock()
        events = OledInputQueue()
        engine = GStreamerPlayback()
        config = AppConfig(server_url="https://books.example.com", token="token", library_id="library-1")
        book = Book("book-1", "The Hobbit", "J.R.R. Tolkien", 300, "")

        def start(session, _client, *, start_over, cancelled, timing) -> None:
            engine.session = session
            engine.current_time = session.current_time
            engine.is_playing = True
            timing.observe_output("output_observed", rendered_at=timing.clock())

        engine.start = Mock(side_effect=start)
        frames = []
        main_player = None
        more_button = None
        more_menu = None

        def after_frame(shutdown, seconds) -> None:
            nonlocal main_player, more_button, more_menu
            if seconds <= 0:
                return
            frame = display.show.call_args.args[0]
            first_frame = not frames
            frames.append(frame)
            if first_frame and isinstance(frame, HomeFrame):
                events.put(InputEvent("nav_click"))
                events.put(InputEvent("nav_click"))
            elif isinstance(frame, PlayingFrame) and main_player is None:
                main_player = frame
                events.put(InputEvent("nav", steps=2))
            elif isinstance(frame, PlayingFrame) and frame.selected_index == 4 and more_button is None:
                more_button = frame
                events.put(InputEvent("nav_click"))
            elif isinstance(frame, TwoLineFrame) and frame.top == "More":
                more_menu = frame
                shutdown.request(0, None)
            if len(frames) > 100 or frame.top == "OLED error":
                shutdown.request(0, None)
            time.sleep(0.001)

        save = Mock(side_effect=PermissionError("Playback state is read-only"))
        record_timing = Mock()
        with (
            patch("abs_kids_player.playback_start.load_config", return_value=config),
            patch("abs_kids_player.playback_start.AudiobookshelfClient", FakePlaybackClient),
        ):
            self.run_service(
                display, events, config, after_frame,
                GStreamerPlayback=Mock(return_value=engine),
                load_cached_books=Mock(return_value=[book]),
                start_book_load_worker=Mock(),
                wifi_status=Mock(return_value=WifiStatus(connected=True, ssid="home")),
                save_last_playback=save,
                record_startup_sample=record_timing,
            )

        save.assert_called_once()
        self.assertEqual(record_timing.call_count, 1)
        sample = record_timing.call_args.args[0]
        self.assertEqual((sample.source, sample.outcome), ("audiobookshelf", "output_observed"))
        self.assertNotIn("OLED error", [frame.top for frame in frames])
        self.assertEqual(main_player.actions, ("Home", "Prev", "Pause", "Next", "..."))
        self.assertEqual(more_button.actions, main_player.actions)
        self.assertEqual(more_button.chapter_text, main_player.chapter_text)
        self.assertEqual(more_menu, TwoLineFrame("More", "Back"))

    def test_quiet_volume_is_saved_before_the_volume_overlay_dismisses(self) -> None:
        display = Mock()
        events = OledInputQueue()
        events.put(InputEvent("volume", steps=1))
        saved_status = Mock()

        def stop_after_input(shutdown, seconds) -> None:
            if seconds > 0:
                shutdown.request(0, None)

        self.run_service(
            display,
            events,
            AppConfig(),
            stop_after_input,
            load_web_player_status=Mock(return_value=WebPlayerStatus(volume_percent=2)),
            save_web_player_status=saved_status,
            wifi_status=Mock(return_value=WifiStatus(connected=False)),
        )

        display.show.assert_any_call(VolumeFrame(percent=3))
        self.assertEqual(saved_status.call_args.args[0].volume_percent, 3)

    def test_new_service_instances_restore_quiet_zero_and_muted_volume_from_disk(self) -> None:
        def stop_after_frame(shutdown, seconds) -> None:
            if seconds > 0:
                shutdown.request(0, None)

        for initial, event, expected in (
            (4, InputEvent("volume", steps=-1), VolumeState(3)),
            (1, InputEvent("volume", steps=-1), VolumeState(0)),
            (3, InputEvent("volume_click"), VolumeState(3, muted=True)),
        ):
            with self.subTest(volume=expected), tempfile.TemporaryDirectory() as directory:
                volume_file = str(Path(directory) / "player-state.json")
                with patch.dict("os.environ", {"ABS_KIDS_PLAYER_STATE_PATH": volume_file}):
                    events = OledInputQueue()
                    events.put(event)
                    self.run_service(
                        Mock(), events, AppConfig(), stop_after_frame,
                        load_saved_volume=oled_service.load_saved_volume,
                        save_output_volumes=oled_service.save_output_volumes,
                        load_web_player_status=Mock(return_value=WebPlayerStatus(volume_percent=initial)),
                        wifi_status=Mock(return_value=WifiStatus(connected=False)),
                    )

                    status = Mock()
                    stale_status = Mock(return_value=WebPlayerStatus(volume_percent=50))
                    chime = Mock()
                    self.run_service(
                        Mock(), OledInputQueue(), AppConfig(), stop_after_frame,
                        load_saved_volume=oled_service.load_saved_volume,
                        save_output_volumes=oled_service.save_output_volumes,
                        load_web_player_status=stale_status,
                        save_web_player_status=status,
                        play_boot_chime=chime,
                        wifi_status=Mock(return_value=WifiStatus(connected=False)),
                    )
                    stale_status.assert_not_called()
                    self.assertEqual(status.call_args.args[0].volume_percent, expected.percent)
                    self.assertEqual(status.call_args.args[0].muted, expected.muted or expected.percent == 0)
                    if expected.muted or expected.percent == 0:
                        chime.assert_not_called()
                    else:
                        chime.assert_called_once_with(expected.percent)

    def test_screen_saver_uses_lower_panel_contrast_and_restores_it_on_wake(self) -> None:
        display = Mock()

        def stop_after_two_frames(shutdown, seconds) -> None:
            if seconds > 0 and display.set_brightness.call_count == 2:
                shutdown.request(0, None)
            if display.show.call_count > 6:
                shutdown.request(0, None)

        with patch("abs_kids_player.oled_presentation.screen_saver_is_active", side_effect=[True, False]):
            self.run_service(
                display, OledInputQueue(), AppConfig(screen_saver_dim_percent=5), stop_after_two_frames,
                wifi_status=Mock(return_value=WifiStatus(connected=False)),
            )

        self.assertEqual([call.args[0] for call in display.set_brightness.call_args_list], [73, 100])
        frames = [call.args[0] for call in display.show.call_args_list]
        saver = next(frame for frame in frames if isinstance(frame, ScreenSaverFrame))
        self.assertEqual(saver.brightness_percent, 5)

    def test_brightness_change_is_retried_after_a_failed_transfer(self) -> None:
        display = Mock()
        display.set_brightness.side_effect = [OSError("SPI unavailable"), None]

        current = update_display_brightness(display, 73, 100)
        self.assertEqual(current, 100)
        self.assertEqual(update_display_brightness(display, 73, current), 73)

    def test_idle_screen_off_keeps_web_volume_playback_and_display_heartbeat_working(self):
        clock = Mock(wraps=time)
        clock.monotonic.return_value = 1000
        display, events, status = Mock(), OledInputQueue(), Mock()
        engine = GStreamerPlayback()
        engine.session = make_playback_session()
        engine.is_playing = True
        engine.poll = Mock()
        commands = []
        frames = []
        levels = []

        def next_frame(shutdown, seconds):
            if seconds <= 0:
                return
            frames.append(display.show.call_args.args[0])
            live = status.call_args.args[0]
            levels.append(live.volume_percent)
            self.assertTrue(live.is_playing)
            self.assertGreater(live.display_drawn_at, 0)
            if len(frames) == 1:
                clock.monotonic.return_value += 61
            elif len(frames) == 2:
                commands.append(WebPlayerCommand("volume", value=31))
            elif len(frames) == 3:
                events.put(InputEvent("volume_click", received_at=1061))
            else:
                shutdown.request(0, None)

        def read_commands():
            batch = commands.copy()
            commands.clear()
            return batch

        self.run_service(
            display, events, AppConfig(screen_off_minutes=1), next_frame, time=clock,
            wifi_status=Mock(return_value=WifiStatus(connected=False)),
            GStreamerPlayback=Mock(return_value=engine), consume_web_player_commands=read_commands,
            save_web_player_status=status, save_playback_state=Mock(), ListeningDetailsPublisher=Mock(),
        )
        self.assertNotEqual(frames[0], SCREEN_OFF)
        self.assertEqual(frames[1:3], [SCREEN_OFF, SCREEN_OFF])
        self.assertNotEqual(frames[3], SCREEN_OFF)
        self.assertEqual(levels[-2:], [31, 31])
        self.assertFalse(status.call_args.args[0].muted)
        engine.poll.assert_called()

    def test_boot_during_screen_off_hours_wakes_on_first_input_and_sleeps_again(self):
        clock, local_clock = Mock(wraps=time), Mock()
        clock.monotonic.return_value = 500
        local_clock.now.return_value = datetime(2026, 11, 25, 1)
        display, events, frames = Mock(), OledInputQueue(), []

        def next_frame(shutdown, seconds):
            frames.append(display.show.call_args.args[0])
            if len(frames) == 2:
                events.put(InputEvent("volume", 1, received_at=500))
            elif len(frames) == 3:
                clock.monotonic.return_value += 61
            elif len(frames) == 4:
                local_clock.now.return_value = datetime(2026, 11, 25, 7)
            elif len(frames) == 5:
                shutdown.request(0, None)

        self.run_service(display, events, AppConfig(screen_off_start="22:00", screen_off_end="07:00"),
                         next_frame, time=clock, datetime=local_clock,
                         wifi_status=Mock(return_value=WifiStatus(connected=False)))
        self.assertEqual([frame == SCREEN_OFF for frame in frames], [True, True, False, True, False])
        self.assertFalse(any(isinstance(frame, VolumeFrame) for frame in frames))

    def test_spoken_navigation_and_clicks_use_their_own_level_at_zero_playback_volume(self) -> None:
        events = OledInputQueue()
        events.put(InputEvent("nav_click"))
        speech = Mock()
        click = Mock()

        def stop_after_frame(shutdown, seconds) -> None:
            if seconds > 0:
                shutdown.request(0, None)

        self.run_service(
            Mock(),
            events,
            AppConfig(
                spoken_navigation_enabled=True,
                control_click_volume_percent=18,
                spoken_navigation_volume_percent=42,
            ),
            stop_after_frame,
            load_saved_volume=Mock(return_value=VolumeState(0)),
            load_cached_books=Mock(return_value=[Book("book-1", "The Hobbit", "", 1, "")]),
            SpokenNavigationFeedback=Mock(return_value=speech),
            play_control_click=click,
            wifi_status=Mock(return_value=WifiStatus(connected=False)),
        )

        click.assert_called_once_with(18)
        self.assertEqual(speech.request.call_args.kwargs["volume_percent"], 42)
        self.assertFalse(speech.request.call_args.kwargs.get("muted", False))

    def test_shutdown_during_discovery_closes_display_even_if_playback_stop_fails(self):
        scanning, released = threading.Event(), threading.Event()

        def discover(publish, cancel):
            scanning.set()
            if cancel.wait(2):
                released.set()

        def make_menu(*args, **kwargs):
            menu = ApplianceMenu(*args, **kwargs)
            menu.speakers = SpeakerPicker(discover=discover)
            menu.screen = Screen.BLUETOOTH_DEVICES
            menu.speakers.open()
            return menu

        events = OledInputQueue()
        display = Mock()
        display.close.side_effect = lambda: self.assertTrue(released.is_set())
        engine = GStreamerPlayback()
        engine.stop = Mock(side_effect=OSError("audio shutdown failed"))

        def stop_after_frame(shutdown, seconds):
            if seconds > 0:
                self.assertTrue(scanning.wait(1))
                shutdown.request(0, None)

        with self.assertRaisesRegex(OSError, "audio shutdown failed"):
            self.run_service(display, events, AppConfig(), stop_after_frame,
                             GStreamerPlayback=Mock(return_value=engine), ApplianceMenu=make_menu,
                             wifi_status=Mock(return_value=WifiStatus(connected=False)))
        engine.stop.assert_called_once_with()
        display.close.assert_called_once_with()

    def test_warmup_runs_once_after_the_first_non_splash_frame_is_drawn(self) -> None:
        display, runtime = Mock(), Mock()
        frames_at_warmup = []
        runtime.start_warmup.side_effect = lambda: frames_at_warmup.extend(
            call.args[0] for call in display.show.call_args_list
        )

        def stop_after_draws(shutdown, seconds):
            if display.show.call_count >= 4:
                shutdown.request(0, None)

        self.run_service(
            display, OledInputQueue(), AppConfig(), stop_after_draws,
            GStreamerRuntime=Mock(return_value=runtime),
            wifi_status=Mock(return_value=WifiStatus(connected=False)),
        )
        runtime.start_warmup.assert_called_once_with()
        runtime.stop_preloading.assert_called_once_with()
        self.assertIsInstance(frames_at_warmup[0], SplashFrame)
        self.assertNotIsInstance(frames_at_warmup[-1], SplashFrame)
        self.assertLess(len(frames_at_warmup), display.show.call_count)

    def run_service(self, display, events, config, after_frame, **overrides) -> None:
        catalogue_mocks = {name: overrides.pop(name, Mock()) for name in ("save_cached_books", "report_catalogue_result")}
        for name in ("update_config", "start_book_load_worker"):
            if name in overrides:
                catalogue_mocks[name] = overrides.pop(name)
        catalogue_mocks["load_config"] = overrides.get("load_config", Mock(return_value=config))
        if "save_command_result" in overrides:
            catalogue_mocks["save_command_result"] = overrides["save_command_result"]
        mocks = {
            "GStreamerRuntime": Mock(),
            "OledInputQueue": Mock(return_value=events),
            "enable_stack_dump_signal": Mock(),
            "start_bluetooth_status_worker": Mock(),
            "load_config": Mock(return_value=config),
            "load_cached_books": Mock(return_value=[]),
            "DeviceStatusCache": Mock(return_value=Mock(current=lambda probe: probe())),
            "record_startup_sample": Mock(),
            "load_player_state": Mock(return_value=None),
            "load_bluetooth_volume": Mock(return_value=VolumeState(10)),
            "load_saved_volume": Mock(return_value=None),
            "save_output_volumes": Mock(),
            "load_web_player_status": Mock(return_value=WebPlayerStatus()),
            "save_web_player_status": Mock(),
            "update_state_is_active": Mock(return_value=False),
            "consume_web_player_commands": Mock(return_value=[]),
            "play_boot_chime": Mock(),
            "sleep_until_shutdown": Mock(side_effect=lambda shutdown, seconds, **_options: after_frame(shutdown, seconds)),
            **overrides,
        }
        with (
            patch.object(oled_service.signal, "signal"),
            patch.multiple(oled_catalogue, **catalogue_mocks),
            patch.multiple(oled_service, **mocks),
        ):
            oled_service.run_oled_service(
                display, splash_seconds=0, startup_network_grace_seconds=0,
                nav_input_enabled=False, volume_input_enabled=False,
            )

    def test_splash_frame_says_chapter(self) -> None:
        frame = splash_frame()

        self.assertIsInstance(frame, SplashFrame)
        self.assertEqual(frame.text, "chapter")

    def test_software_update_frame_warns_against_powering_off(self) -> None:
        self.assertEqual(SOFTWARE_UPDATE_FRAME.top, "UPDATING")
        self.assertEqual(SOFTWARE_UPDATE_FRAME.bottom, "DO NOT POWER OFF")

    def test_late_bluetooth_probe_preserves_the_output_selected_by_the_listener(self) -> None:
        bluetooth, menu = OledBluetooth(), ApplianceMenu()
        playback, audio_test = Mock(), Mock()
        command = MenuCommand(CommandType.SET_BLUETOOTH_ENABLED, bluetooth_enabled=True)
        address = "00:11:22:33:44:55"

        bluetooth.apply_startup_status(menu, playback, [command])
        bluetooth.results.put(
            BluetoothActionResult(
                True,
                "Connected",
                enabled=True,
                connected=True,
                device_address=address,
            )
        )
        bluetooth.apply_actions(menu, playback, audio_test)
        bluetooth.status_results.put(BluetoothStatusResult(enabled=False, connected=False))
        changed = bluetooth.apply_startup_status(menu, playback, [])

        self.assertFalse(changed)
        self.assertTrue(menu.bluetooth_connected)
        self.assertEqual(audio_test.bluetooth_address, address)
        playback.set_bluetooth_output_enabled.assert_called_once_with(True, address)
        playback.refresh_audio_output.assert_called_once_with()

    def test_update_discards_queued_dial_turn_then_controls_resume(self) -> None:
        display, events = Mock(), OledInputQueue()
        events.put(InputEvent("volume", 1))
        web_commands = Mock(return_value=[])
        acknowledge = Mock(return_value=True)
        frames = []
        reads_after_frame = []

        def after_frame(shutdown, seconds):
            if seconds == 0:
                return  # The splash delay happens before the first service iteration.
            frames.append(display.show.call_args.args[0])
            reads_after_frame.append(web_commands.call_count)
            if len(frames) == 1:
                events.put(InputEvent("volume", 1))
            else:
                shutdown.request(0, None)

        self.run_service(
            display,
            events,
            AppConfig(),
            after_frame,
            load_saved_volume=lambda: VolumeState(3),
            wifi_status=lambda: WifiStatus(False),
            DEFAULT_SOFTWARE_UPDATE_REFRESH_SECONDS=0,
            load_update_state=Mock(
                side_effect=[SoftwareUpdateState(phase="installing"), SoftwareUpdateState()]
            ),
            update_state_is_active=lambda state: True,
            mark_update_display_ready=acknowledge,
            consume_web_player_commands=web_commands,
        )

        self.assertEqual(frames, [SOFTWARE_UPDATE_FRAME, VolumeFrame(4)])
        self.assertEqual(reads_after_frame, [0, 1])
        acknowledge.assert_called_once_with()

    def test_signal_boot_splash_handoff_writes_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff"
            with patch("abs_kids_player.oled_service.time.sleep") as sleep:
                signal_boot_splash_handoff(str(path), settle_seconds=0.2)

            self.assertEqual(path.read_text(encoding="utf-8"), "starting\n")
            sleep.assert_called_once_with(0.2)

    def test_setup_hotspot_shows_setup_instructions(self) -> None:
        frame = frame_for_wifi_status(WifiStatus(connected=True, ssid=SETUP_HOTSPOT_SSID))

        self.assertIsInstance(frame, WifiSetupFrame)
        self.assertEqual(frame.ssid, SETUP_HOTSPOT_SSID)
        self.assertEqual(frame.url, SETUP_HOTSPOT_URL)

    def test_home_wifi_shows_setup_address(self) -> None:
        frame = frame_for_wifi_status(WifiStatus(connected=True, ssid="Home WiFi"), lan_ip="192.168.1.42")

        self.assertIsInstance(frame, TwoLineFrame)
        self.assertEqual(frame.top, "Setup address")
        self.assertEqual(frame.bottom, "http://192.168.1.42")

    def test_disconnected_shows_setup_instructions(self) -> None:
        frame = frame_for_wifi_status(WifiStatus(connected=False, message="Not connected"))

        self.assertIsInstance(frame, WifiSetupFrame)
        self.assertEqual(frame.ssid, SETUP_HOTSPOT_SSID)

    def test_boot_state_uses_home_frame_on_home_wifi(self) -> None:
        home = TwoLineFrame("The Hobbit", "By J.R.R. Tolkien")

        frame = frame_for_boot_state(WifiStatus(connected=True, ssid="Home WiFi"), home)

        self.assertEqual(frame, home)

    def test_boot_state_keeps_splash_while_network_is_starting(self) -> None:
        frame = frame_for_boot_state(
            WifiStatus(connected=False, message="NetworkManager starting"),
            startup_waiting=True,
        )

        self.assertIsInstance(frame, SplashFrame)
        self.assertEqual(frame.text, "chapter")

    def test_boot_state_shows_setup_after_network_grace_expires(self) -> None:
        frame = frame_for_boot_state(
            WifiStatus(connected=False, message="Not connected"),
            startup_waiting=False,
        )

        self.assertIsInstance(frame, WifiSetupFrame)

    def test_boot_state_keeps_the_cached_library_visible_without_wifi(self) -> None:
        home = HomeFrame("The Hobbit", "J.R.R. Tolkien")

        frame = frame_for_boot_state(WifiStatus(connected=False), home, startup_waiting=True)

        self.assertEqual(frame, home)

    def test_boot_state_shows_active_setup_hotspot_during_network_grace(self) -> None:
        frame = frame_for_boot_state(
            WifiStatus(connected=True, ssid=SETUP_HOTSPOT_SSID),
            startup_waiting=True,
        )

        self.assertIsInstance(frame, WifiSetupFrame)

    def test_home_frame_for_books_uses_library_home_screen(self) -> None:
        frame = home_frame_for_books(
            [Book(id="book-1", title="The Hobbit", author="J.R.R. Tolkien", duration=1, cover_url="")]
        )

        self.assertIsInstance(frame, HomeFrame)
        self.assertEqual(frame.top, "The Hobbit")
        self.assertEqual(frame.bottom, "By J.R.R. Tolkien")

    def test_load_home_frame_loads_books_from_configured_audiobookshelf(self) -> None:
        frame = load_home_frame(
            AppConfig(server_url="https://books.example.com", token="token", library_id="library-1"),
            client_factory=FakeClient,
        )

        self.assertEqual(frame.top, "The Hobbit")
        self.assertEqual(frame.bottom, "By J.R.R. Tolkien")

    def test_load_home_frame_without_login_shows_setup_address(self) -> None:
        frame = load_home_frame(AppConfig(), lan_ip="192.168.1.42")

        self.assertEqual(frame.top, "Setup address")
        self.assertEqual(frame.bottom, "http://192.168.1.42")

    def test_book_load_result_returns_books_for_configured_audiobookshelf(self) -> None:
        result = book_load_result(
            AppConfig(server_url="https://books.example.com", token="token", library_id="library-1"),
            client_factory=FakeClient,
        )

        self.assertIsInstance(result, BookLoadResult)
        self.assertIsNotNone(result.books)
        self.assertEqual([book.title for book in result.books], ["The Hobbit"])
        self.assertIsNone(result.error_frame)

    def test_book_load_result_turns_audiobookshelf_error_into_display_frame(self) -> None:
        result = book_load_result(
            AppConfig(server_url="https://books.example.com", token="token", library_id="library-1"),
            client_factory=FakeFailingClient,
        )

        self.assertIsNone(result.books)
        self.assertIsInstance(result.error_frame, TwoLineFrame)
        self.assertEqual(result.error_frame.top, "Books unavailable")
        self.assertEqual(result.error_frame.bottom, "Press to retry")

    def test_start_book_load_worker_queues_book_result(self) -> None:
        results: queue.SimpleQueue[BookLoadResult] = queue.SimpleQueue()

        start_book_load_worker(
            AppConfig(server_url="https://books.example.com", token="token", library_id="library-1"),
            results,
            client_factory=FakeClient,
        )
        result = results.get(timeout=1)

        self.assertIsNotNone(result.books)
        self.assertEqual(result.books[0].title, "The Hobbit")

    def test_save_playback_state_persists_active_session(self) -> None:
        playback = FakePlayback(make_playback_session())
        playback.current_time = 88

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with patch.dict("os.environ", {"ABS_KIDS_PLAYER_STATE_PATH": str(path)}):
                saved = save_playback_state(playback)
                state = load_player_state()

        self.assertTrue(saved)
        self.assertIsNotNone(state)
        self.assertEqual(state.book_id, "book-1")
        self.assertEqual(state.title, "The Hobbit")
        self.assertEqual(state.current_time, 88)

    def test_poll_playback_safely_returns_frame_on_playback_error(self) -> None:
        frame = poll_playback_safely(FakePollingErrorPlayback())

        self.assertIsInstance(frame, TwoLineFrame)
        self.assertEqual(frame.top, "Playback stopped")
        self.assertEqual(frame.bottom, "Check audio")

    def test_web_status_for_playback_reports_active_session(self) -> None:
        playback = FakePlayback(make_playback_session())
        playback.current_time = 88
        playback.is_playing = True

        status = web_status_for_playback(playback, volume_percent=55, muted=False)

        self.assertEqual(status.title, "The Hobbit")
        self.assertTrue(status.is_playing)
        self.assertEqual(status.current_time, 88)
        self.assertEqual(status.volume_percent, 55)

    def test_sleep_transition_publishes_paused_player_status(self) -> None:
        session = make_playback_session()
        playback = FakePlayback(session)
        playback.current_time = 88
        playback.is_playing = True
        menu = ApplianceMenu([Book("book-1", "The Hobbit", "J.R.R. Tolkien", 300, "")])
        menu.set_session(session, is_playing=True)
        timer = ListeningSleepTimer()
        publisher = WebStatusPublisher(oled_service.save_web_player_status)
        event = timer.update(60, 60, True, 1, session.id, True)

        with tempfile.TemporaryDirectory() as directory:
            status_path = str(Path(directory) / "status.json")
            with patch.dict("os.environ", {"ABS_KIDS_PLAYER_WEB_STATUS_PATH": status_path}):
                apply_sleep_timer_event(event, timer, 60, playback, menu, volume_percent=55, muted=False, publisher=publisher)
                status = load_web_player_status()

        self.assertEqual(playback.pause_calls, 1)
        self.assertFalse(playback.is_playing)
        self.assertFalse(menu.is_playing)
        self.assertFalse(status.is_playing)
        self.assertEqual(status.current_time, 88)
        self.assertEqual(menu.render(), SleepConfirmFrame(30))

        event = timer.update(61, 1, False, 1, session.id, False)
        apply_sleep_timer_event(event, timer, 61, playback, menu, volume_percent=55, muted=False, publisher=publisher)

        self.assertEqual(playback.resume_calls, 1)
        self.assertTrue(menu.is_playing)
        self.assertIsInstance(menu.render(), PlayingFrame)

    def test_web_player_commands_pause_play_and_adjust_volume(self) -> None:
        menu = ApplianceMenu([Book("book-1", "The Hobbit", "J.R.R. Tolkien", 300, "")])
        session = make_playback_session()
        playback = FakePlayback(session)
        playback.current_time = 88
        playback.is_playing = True
        menu.set_session(session, is_playing=True)

        with tempfile.TemporaryDirectory() as directory:
            command_path = str(Path(directory) / "commands.json")
            with patch.dict("os.environ", {"ABS_KIDS_PLAYER_WEB_COMMANDS_PATH": command_path}):
                queue_web_player_command("pause")
                volume, muted, changed, refresh_library = handle_web_player_commands(playback, menu, 50, False)

                self.assertTrue(changed)
                self.assertFalse(refresh_library)
                self.assertEqual(playback.pause_calls, 1)
                self.assertFalse(menu.is_playing)
                self.assertEqual((volume, muted), (50, False))

                queue_web_player_command("play")
                queue_web_player_command("volume", 25)
                volume, muted, changed, refresh_library = handle_web_player_commands(playback, menu, volume, muted)

        self.assertTrue(changed)
        self.assertFalse(refresh_library)
        self.assertEqual(playback.resume_calls, 1)
        self.assertTrue(menu.is_playing)
        self.assertEqual((volume, muted), (25, False))
        self.assertEqual(playback.volume_states[-1], (25, False))

    def test_web_refresh_library_command_pauses_stops_and_returns_home(self) -> None:
        menu = ApplianceMenu([Book("book-1", "The Hobbit", "J.R.R. Tolkien", 300, "")])
        session = make_playback_session()
        playback = FakePlayback(session)
        playback.current_time = 88
        playback.is_playing = True
        menu.set_session(session, is_playing=True)

        with tempfile.TemporaryDirectory() as directory:
            command_path = str(Path(directory) / "commands.json")
            state_path = str(Path(directory) / "last-playback.json")
            with patch.dict(
                "os.environ",
                {
                    "ABS_KIDS_PLAYER_WEB_COMMANDS_PATH": command_path,
                    "ABS_KIDS_PLAYER_STATE_PATH": state_path,
                },
            ):
                save_last_playback("book-1", "The Hobbit", 88)
                queue_web_player_command("refresh_library")
                volume, muted, changed, refresh_library = handle_web_player_commands(playback, menu, 50, False)

                self.assertTrue(changed)
                self.assertTrue(refresh_library)
                self.assertEqual(playback.pause_calls, 1)
                self.assertEqual(playback.stop_calls, 1)
                self.assertIsNone(playback.session)
                self.assertEqual(menu.screen, "library")
                self.assertFalse(menu.books)
                self.assertIsNone(load_player_state())
                self.assertEqual((volume, muted), (50, False))

    def test_manual_catalogue_command_leaves_playback_running(self) -> None:
        book = Book("book-1", "The Hobbit", "", 300, "")
        menu = ApplianceMenu([book])
        session = make_playback_session()
        playback = FakePlayback(session)
        playback.is_playing = True
        menu.set_session(session, is_playing=True)
        request = Mock()
        command = WebPlayerCommand(CATALOGUE_COMMAND, id="manual")
        with patch("abs_kids_player.oled_service.consume_web_player_commands", return_value=[command]):
            result = handle_web_player_commands(playback, menu, 40, False, request_catalogue=request)
        request.assert_called_once_with(command)
        self.assertEqual(result, (40, False, False, False))
        self.assertIs(playback.session, session)
        self.assertTrue(playback.is_playing)
        self.assertEqual((playback.pause_calls, playback.stop_calls), (0, 0))
        menu.set_books([])
        self.assertIsInstance(menu.render(), PlayingFrame)

    def test_web_resort_library_command_keeps_playback_running(self) -> None:
        menu = ApplianceMenu(
            [
                Book("book-1", "The Hobbit", "J.R.R. Tolkien", 300, ""),
                Book("book-2", "Matilda", "Roald Dahl", 300, ""),
            ]
        )
        session = make_playback_session()
        playback = FakePlayback(session)
        playback.is_playing = True
        menu.set_session(session, is_playing=True)

        with tempfile.TemporaryDirectory() as directory:
            command_path = str(Path(directory) / "commands.json")
            with (
                patch.dict("os.environ", {"ABS_KIDS_PLAYER_WEB_COMMANDS_PATH": command_path}),
                patch(
                    "abs_kids_player.oled_service.load_config",
                    return_value=AppConfig(library_sort_mode=LIBRARY_SORT_AUTHOR),
                ),
            ):
                queue_web_player_command("resort_library")
                volume, muted, changed, refresh_library = handle_web_player_commands(
                    playback,
                    menu,
                    50,
                    False,
                )

        self.assertTrue(changed)
        self.assertFalse(refresh_library)
        self.assertEqual([book.title for book in menu.books], ["Matilda", "The Hobbit"])
        self.assertEqual(menu.library_sort_mode, LIBRARY_SORT_AUTHOR)
        self.assertTrue(playback.is_playing)
        self.assertEqual(playback.pause_calls, 0)
        self.assertEqual(playback.stop_calls, 0)
        self.assertEqual((volume, muted), (50, False))

    def test_home_title_does_not_scroll_before_delay(self) -> None:
        frame = HomeFrame("A Very Long Audiobook Title", "Author")

        self.assertEqual(scrolled_home_frame(frame, elapsed_seconds=1.9), frame)

    def test_home_title_scrolls_after_delay(self) -> None:
        frame = scrolled_home_frame(
            HomeFrame(
                "A Very Long Audiobook Title",
                "Author",
                series_position="2/7",
                section_letter="A",
                section_letter_fill=128,
                is_podcast=True,
            ),
            elapsed_seconds=3.0,
        )

        self.assertIsInstance(frame, HomeFrame)
        self.assertTrue(frame.title_scrolling)
        self.assertEqual(frame.title_scroll_px, 16)
        self.assertEqual(frame.section_letter, "A")
        self.assertEqual(frame.section_letter_fill, 128)
        self.assertEqual(frame.series_position, "2/7")
        self.assertTrue(frame.is_podcast)

    def test_home_cache_refresh_keeps_scroll_timer_when_book_is_same(self) -> None:
        old = HomeFrame("A Very Long Audiobook Title", "Author")
        new = HomeFrame("A Very Long Audiobook Title", "Author", section_letter="A", section_letter_fill=128)

        frame, loaded_at = refresh_home_cache(old, new, home_frame_loaded_at=12.0, now=99.0)

        self.assertEqual(frame, new)
        self.assertEqual(loaded_at, 12.0)

    def test_home_cache_refresh_resets_scroll_timer_when_book_changes(self) -> None:
        old = HomeFrame("Old Title", "Author")
        new = HomeFrame("New Title", "Author")

        frame, loaded_at = refresh_home_cache(old, new, home_frame_loaded_at=12.0, now=99.0)

        self.assertEqual(frame, new)
        self.assertEqual(loaded_at, 99.0)

    def test_home_cache_refresh_resets_scroll_timer_when_series_position_changes(self) -> None:
        old = HomeFrame("Book Two", "Author", series_position="2/6")
        new = HomeFrame("Book Two", "Author", series_position="2/7")

        frame, loaded_at = refresh_home_cache(old, new, home_frame_loaded_at=12.0, now=99.0)

        self.assertEqual(frame, new)
        self.assertEqual(loaded_at, 99.0)

    def test_still_listening_confirmation_resumes_playback(self) -> None:
        menu = ApplianceMenu([Book("book-1", "The Hobbit", "J.R.R. Tolkien", 300, "")])
        session = make_playback_session()
        menu.set_session(session, is_playing=False)
        menu.show_sleep_confirmation(30)
        playback = FakePlayback(session)

        overlay = handle_menu_command(
            MenuCommand(CommandType.CONFIRM_STILL_LISTENING),
            menu,
            playback,
        )

        self.assertIsNone(overlay)
        self.assertEqual(playback.resume_calls, 1)
        self.assertTrue(playback.is_playing)
        self.assertTrue(menu.is_playing)

    def test_mute_indicator_is_added_to_non_volume_frames_only(self) -> None:
        home = frame_with_mute_indicator(HomeFrame("Matilda", "Roald Dahl"), muted=True)
        volume_frame = VolumeFrame(50, muted=True)
        volume = frame_with_mute_indicator(volume_frame, muted=True)

        self.assertIsInstance(home, HomeFrame)
        self.assertTrue(home.muted)
        self.assertIs(volume, volume_frame)

    def test_status_indicator_adds_bluetooth_connection_to_frames(self) -> None:
        frame = frame_with_status_indicators(HomeFrame("Matilda", "Roald Dahl"), muted=False, bluetooth_connected=True)

        self.assertIsInstance(frame, HomeFrame)
        self.assertTrue(frame.bluetooth_connected)
        self.assertFalse(frame.muted)

    def test_bluetooth_success_frame_reports_ready_speaker(self) -> None:
        frame = bluetooth_result_frame(
            BluetoothActionResult(True, "Bluetooth connected", enabled=True, device_name="Playroom Speaker")
        )

        self.assertEqual(frame.top, "Speaker ready")
        self.assertEqual(frame.bottom, "Playroom Speaker")

    def test_bluetooth_unpair_frame_returns_to_internal_speaker(self) -> None:
        frame = bluetooth_result_frame(
            BluetoothActionResult(
                True,
                "Bluetooth unpaired",
                enabled=True,
                connected=False,
                device_name="Playroom Speaker",
            )
        )

        self.assertEqual(frame.top, "Disconnected")
        self.assertEqual(frame.bottom, "Internal speaker")

    def test_amp_shutdown_tracks_muted_or_zero_volume(self) -> None:
        amp = FakeAmpShutdown()

        self.assertTrue(set_amp_shutdown(amp, muted=True, volume_percent=50))
        self.assertTrue(set_amp_shutdown(amp, muted=False, volume_percent=0))
        self.assertFalse(set_amp_shutdown(amp, muted=False, volume_percent=50))
        self.assertFalse(set_amp_shutdown(amp, muted=True, volume_percent=0, feedback_active=True))

        self.assertEqual(amp.states, [True, True, False, False])

    def test_muted_play_command_unmutes_and_consumes_toggle(self) -> None:
        command = MenuCommand(CommandType.TOGGLE_PLAYBACK)

        commands, percent, muted, unmuted, resume = unmute_for_audible_playback_commands(
            [command],
            fallback_percent=50,
            fallback_muted=True,
            software_volume_only=True,
        )

        self.assertEqual(commands, [])
        self.assertEqual(percent, 50)
        self.assertFalse(muted)
        self.assertTrue(unmuted)
        self.assertTrue(resume)

    def test_muted_play_book_command_unmutes_but_still_starts_book(self) -> None:
        command = MenuCommand(CommandType.PLAY_BOOK, book_id="book-1")

        commands, percent, muted, unmuted, resume = unmute_for_audible_playback_commands(
            [command],
            fallback_percent=0,
            fallback_muted=False,
            software_volume_only=True,
        )

        self.assertEqual(commands, [command])
        self.assertEqual(percent, DEFAULT_UNMUTE_VOLUME_PERCENT)
        self.assertFalse(muted)
        self.assertTrue(unmuted)
        self.assertFalse(resume)

    def test_continue_current_book_returns_to_active_session_without_restart(self) -> None:
        menu = ApplianceMenu([Book("book-1", "The Hobbit", "J.R.R. Tolkien", 300, "", current_time=60, progress=0.2)])
        playback = FakePlayback(make_playback_session())
        playback.current_time = 137
        playback.is_playing = True

        overlay = handle_menu_command(
            MenuCommand(CommandType.PLAY_BOOK, book_id="book-1", start_over=False, resume_time=60),
            menu,
            playback,
        )

        self.assertIsNone(overlay)
        frame = menu.render()
        self.assertIsInstance(frame, PlayingFrame)
        self.assertEqual(frame.chapter_text, "Roast Mutton")
        self.assertEqual(frame.remaining_text, "-2:43")

    def test_continue_current_book_fetches_history_when_not_playing(self) -> None:
        playback = FakePlayback(make_playback_session())
        playback.current_time = 137
        playback.is_playing = False

        self.assertFalse(
            should_return_to_active_session(
                MenuCommand(CommandType.PLAY_BOOK, book_id="book-1", start_over=False, resume_time=60),
                playback.state,
            )
        )

    def test_pause_playback_command_pauses_active_playback(self) -> None:
        menu = ApplianceMenu([Book("book-1", "The Hobbit", "J.R.R. Tolkien", 300, "")])
        session = make_playback_session()
        menu.set_session(session, is_playing=True)
        playback = FakePlayback(session)
        playback.current_time = 137
        playback.is_playing = True

        overlay = handle_menu_command(MenuCommand(CommandType.PAUSE_PLAYBACK), menu, playback)

        self.assertIsNone(overlay)
        self.assertEqual(playback.pause_calls, 1)
        self.assertFalse(menu.is_playing)

    def test_confirmed_chapter_seek_resumes_when_paused(self) -> None:
        menu = ApplianceMenu([Book("book-1", "The Hobbit", "J.R.R. Tolkien", 300, "")])
        session = make_playback_session()
        menu.set_session(session, is_playing=False)
        playback = FakePlayback(session)
        playback.current_time = 60
        playback.is_playing = False

        overlay = handle_menu_command(
            MenuCommand(CommandType.SEEK_CHAPTER, chapter_index=1, seek_time=120),
            menu,
            playback,
        )

        self.assertIsNone(overlay)
        self.assertEqual(playback.seek_times, [120])
        self.assertEqual(playback.resume_calls, 1)
        self.assertTrue(menu.is_playing)

    def test_clear_last_playback_command_removes_saved_state(self) -> None:
        menu = ApplianceMenu()
        playback = FakePlayback()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with patch.dict("os.environ", {"ABS_KIDS_PLAYER_STATE_PATH": str(path)}):
                save_last_playback("book-1", "The Hobbit", 10)
                overlay = handle_menu_command(
                    MenuCommand(CommandType.CLEAR_LAST_PLAYBACK),
                    menu,
                    playback,
                )

                state = load_player_state()

        self.assertIsNone(overlay)
        self.assertIsNone(state)

    def test_play_book_command_shows_loading_before_starting_session(self) -> None:
        menu = ApplianceMenu([Book("book-1", "The Hobbit", "J.R.R. Tolkien", 300, "", current_time=60, progress=0.2)])
        engine = FakeStartingPlayback()
        playback = PlaybackOwner(engine)
        self.addCleanup(playback.close)

        with (
            patch("abs_kids_player.playback_start.load_config",
                  return_value=AppConfig("https://books.example.com", "test-secret")),
            patch("abs_kids_player.playback_start.AudiobookshelfClient", FakePlaybackClient),
            patch("builtins.print") as printer,
        ):
            overlay = handle_menu_command(
                MenuCommand(CommandType.PLAY_BOOK, book_id="book-1", start_over=False, resume_time=60),
                menu,
                playback,
            )
            result = wait_for_playback_start(playback)

        self.assertEqual(overlay, BOOK_LOADING_FRAME)
        self.assertEqual(result.session.current_time, 60)
        self.assertEqual(len(engine.started_sessions), 1)
        self.assertEqual(engine.start_over_flags, [False])
        printer.assert_called_once()
        line = printer.call_args.args[0]
        self.assertIn("source=audiobookshelf", line)
        self.assertIn("audiobookshelf_ms=", line)
        self.assertIn("gst_ms=", line)
        self.assertIn("select_to_output_ms=", line)
        self.assertNotIn("test-secret", line)
        self.assertNotIn("books.example.com", line)

    def test_configured_podcast_command_starts_latest_podcast_episode(self) -> None:
        podcast = make_podcast_config()
        menu = ApplianceMenu([podcast_book(podcast)])
        engine = FakeStartingPlayback()
        playback = PlaybackOwner(engine)
        self.addCleanup(playback.close)
        session = PlaybackSession(
            id="episode-1",
            library_item_id=podcast.book_id,
            title=podcast.title,
            author=podcast.author,
            duration=1013,
            current_time=0,
            cover_url="",
            tracks=[AudioTrack(1, 0, 1013, "https://example.com/story.mp3", "Draw-Along with Jake")],
            chapters=[Chapter(1, "Draw-Along with Jake", 0, 1013)],
        )

        with (
            patch("abs_kids_player.playback_start.load_config", return_value=AppConfig(podcasts=[podcast])),
            patch("abs_kids_player.playback_start.latest_podcast_session", return_value=session) as latest_session,
            patch("builtins.print") as printer,
        ):
            overlay = handle_menu_command(
                MenuCommand(CommandType.PLAY_BOOK, book_id=podcast.book_id, start_over=True),
                menu,
                playback,
            )
            result = wait_for_playback_start(playback)

        self.assertEqual(overlay, BOOK_LOADING_FRAME)
        latest_session.assert_called_once_with(
            podcast, resume_time=0, start_over=True, episode_id="", account="local-rss"
        )
        self.assertEqual(result.session, session)
        self.assertEqual(engine.started_sessions, [session])
        self.assertEqual(engine.start_over_flags, [True])
        printer.assert_called_once()
        self.assertIn("podcast_ms=", printer.call_args.args[0])

    def test_bluetooth_power_command_starts_worker_and_updates_menu(self) -> None:
        menu = ApplianceMenu()
        playback = FakePlayback()
        results: queue.SimpleQueue[BluetoothActionResult] = queue.SimpleQueue()

        with patch("abs_kids_player.oled_service.start_bluetooth_power_worker") as start_worker:
            overlay = handle_menu_command(
                MenuCommand(CommandType.SET_BLUETOOTH_ENABLED, bluetooth_enabled=False),
                menu,
                playback,
                bluetooth_results=results,
            )

        start_worker.assert_called_once_with(False, results)
        self.assertFalse(menu.bluetooth_enabled)
        self.assertEqual(overlay, BLUETOOTH_POWER_FRAME)


if __name__ == "__main__":
    unittest.main()
