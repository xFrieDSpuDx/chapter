from __future__ import annotations

import math
import os
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable

from .api import AudiobookshelfClient, AudiobookshelfError
from .audio import VolumeState
from .bluetooth_audio import preferred_bluetooth_alsa_device
from .download_store import lock_download, read_download
from .gstreamer_runtime import GStreamerRuntime, PlaybackError
from .gstreamer_runtime import load_gstreamer as load_gstreamer
from .models import PlaybackSession
from .playback_timing import (
    AudioRenderProbe,
    PlaybackTiming,
    find_audio_sink,
    measure_pipeline_setup,
    trace_http_ranges,
)
from .player_state import OutputVolumes, save_last_playback
from .podcast import PodcastProgressClient
from .preroll_probe import PrerollProbe
from .progress_sync import SessionProgress
from .startup_seek import can_seek_to, wait_for_resume_seek


PROGRESS_SYNC_SECONDS = 15
VOLUME_FADE_SECONDS = 0.35
VOLUME_FADE_STEPS = 16
PREROLL_VOLUME_FLOOR = 0.001
SILENCE_VOLUME = 0.0
MAX_EFFECTIVE_VOLUME = 1.0
SPOKEN_NAVIGATION_DUCK_FACTOR = 0.18
PLAY_START_SETTLE_SECONDS = 0.20
RESUME_SETTLE_SECONDS = 0.20
STATE_CHANGE_TIMEOUT_SECONDS = 5
SMART_RESUME_SHORT_PAUSE_SECONDS = 10.0
SMART_RESUME_LONG_PAUSE_SECONDS = 300.0
SMART_RESUME_MEDIUM_REWIND_SECONDS = 3.0
SMART_RESUME_LONG_REWIND_SECONDS = 8.0


class GStreamerPlayback:
    def __init__(self, progress_sync=None, *, runtime: GStreamerRuntime | None = None) -> None:
        self.progress_sync = progress_sync
        self._runtime = runtime if runtime is not None else GStreamerRuntime()
        self.Gst: Any | None = None
        self.playbin: Any | None = None
        self.session: PlaybackSession | None = None
        self.completed_session: PlaybackSession | None = None
        self.client: AudiobookshelfClient | PodcastProgressClient | None = None
        self.track_index = 0
        self.current_time = 0.0
        self.is_playing = False
        self.last_sync = 0.0
        self._last_saved_position: float | None = None
        self._session_progress: SessionProgress | None = None
        self.output_volumes: OutputVolumes | None = None
        self.volume_output = "speaker"
        self.volume_percent = 50
        self.muted = False
        self.soft_paused = False
        self.soft_pause_time = 0.0
        self.soft_paused_at = 0.0
        self.spoken_navigation_ducked = False
        self._sync_lock = threading.Lock()
        self._sync_in_flight = False
        self._audio_lock = threading.Lock()
        self._effective_volume = 0.5
        self.sleep_gain = 1.0
        self.sleep_boundary: float | None = None
        self.night_volume_limit = 100
        self.bluetooth_output_enabled = False
        self.bluetooth_address = ""
        self.audio_output_device = ""
        self.playback_rate = 1.0
        self._startup_probe: AudioRenderProbe | None = None
        self._download_lock = None

    def start(
        self,
        session: PlaybackSession,
        client: AudiobookshelfClient | PodcastProgressClient,
        *,
        start_over: bool = False,
        cancelled: Callable[[], bool] | None = None,
        timing: PlaybackTiming | None = None,
    ) -> None:
        with measure_pipeline_setup(timing, "stop_previous"):
            self.stop()
        if isinstance(session, PlaybackSession) and session.downloaded:
            self._open_download(session)
        gst = self._runtime.load(timing)
        if timing is not None:
            timing.first_pipeline_since_restart = self.Gst is None
        if cancelled is not None and cancelled():
            return
        self.Gst = gst
        with measure_pipeline_setup(timing, "playbin_create"):
            self.playbin = self.Gst.ElementFactory.make("playbin", "chapter-player")
        if self.playbin is None:
            raise PlaybackError("Could not create GStreamer playbin.")
        with measure_pipeline_setup(timing, "tempo_filter"):
            self.configure_tempo_filter()
        with measure_pipeline_setup(timing, "session_setup"):
            self.session = session
            self.client = client
            if isinstance(session, PlaybackSession) and session.progress_account and self.progress_sync is not None:
                self._session_progress = SessionProgress(session.server_checkpoint)
                if session.downloaded and not start_over and session.server_checkpoint is None:
                    self._session_progress = SessionProgress(
                        (session.current_time, session.progress_updated_at), offline_opened_at=time.time_ns() // 1_000_000,
                    )
            self.track_index = self.track_index_for_time(session.current_time)
            self.current_time = session.current_time
            self.soft_paused = False
            self.soft_pause_time = 0.0
            self.soft_paused_at = 0.0
            self.spoken_navigation_ducked = False
            self.set_output_volume(PREROLL_VOLUME_FLOOR)
        with trace_http_ranges(self.playbin, timing):
            self.load_current_track(
                apply_audio=False,
                skip_zero_seek=session.current_time == 0,
                seek_during_preroll=True,
                cancelled=cancelled,
                timing=timing,
            )
            if cancelled is not None and cancelled():
                return
            self.set_output_volume(PREROLL_VOLUME_FLOOR)
            if timing is not None:
                timing.output = "bluetooth" if self.audio_output_device else "default"
                timing.playing_at = timing.clock()
            self.playbin.set_state(self.Gst.State.PLAYING)
            self.is_playing = True
            time.sleep(PLAY_START_SETTLE_SECONDS)
            if cancelled is not None and cancelled():
                return
            if timing is not None:
                if self.target_effective_volume() <= PREROLL_VOLUME_FLOOR:
                    timing.observe_output("muted")
                else:
                    self._startup_probe = AudioRenderProbe(
                        timing, find_audio_sink(self.Gst, self.playbin)
                    )
            self.apply_audio_state(cancelled)
            self.last_sync = time.monotonic()
            if start_over and cancelled is None and self.progress_sync is not None:
                self.request_progress_sync()

    def stop(self) -> None:
        try:
            if self.progress_sync is not None and self.is_playing and self.last_sync:
                self.request_progress_sync()
        finally:
            self.release_pipeline()

    def stop_listening(self) -> float | None:
        """Save the listener's actual place, including a paused place, and close the book."""
        session = self.session
        if session is None:
            return None
        try:
            self._apply_progress_handoff()
            self.set_output_volume(SILENCE_VOLUME)
            if self.is_playing and self.playbin is not None:
                ok, offset = self.playbin.query_position(self.Gst.Format.TIME)
                if ok and offset >= 0:
                    position = self.current_track.start_offset + offset / self.Gst.SECOND
                    self.current_time = min(session.duration, position) if session.duration > 0 else position
            position = self.current_time
            try:
                # A paused book may have moved on another player since we last saved it.
                if self.is_playing or position != self._last_saved_position:
                    self.request_progress_sync()
            finally:
                save_last_playback(session.library_item_id, session.title, position,
                                   episode_id=session.episode_id, account=session.progress_account)
            return position
        finally:
            self.release_pipeline()

    def release_pipeline(self) -> None:
        """Discard audio resources without writing a position for an unaccepted start."""
        with ExitStack() as cleanup:
            # Hold the download lock until GStreamer has been asked to release the file.
            if self._download_lock is not None:
                cleanup.callback(self._download_lock.close)
                self._download_lock = None
            if self.playbin is not None and self.Gst is not None:
                cleanup.callback(self.playbin.set_state, self.Gst.State.NULL)
            self._clear_session()
            self._interrupt_startup_probe()

    def _clear_session(self) -> None:
        self.completed_session = None
        self.playbin = None
        self.session = None
        self.client = None
        self.audio_output_device = ""
        self.current_time = 0.0
        self.is_playing = False
        self.soft_paused = False
        self.soft_pause_time = 0.0
        self.soft_paused_at = 0.0
        self.spoken_navigation_ducked = False
        self.last_sync = 0.0
        self._last_saved_position = None
        self._session_progress = None
        self.sleep_gain = 1.0
        self.sleep_boundary = None

    def _open_download(self, session: PlaybackSession) -> None:
        directory = Path(session.download_directory)
        try:
            lock = lock_download(directory, shared=True)
        except OSError as error:
            raise PlaybackError("The downloaded book is unavailable.") from error
        if not (download := read_download(directory)) or not download.complete:
            lock.close()
            raise PlaybackError("The downloaded book is incomplete.")
        self._download_lock = lock

    def toggle(self) -> None:
        if self.playbin is None or self.Gst is None:
            return
        if self.is_playing:
            self.pause()
            return
        self.resume()

    def resume(self, sync: bool = False, percent: int | None = None, muted: bool | None = None) -> None:
        if self.playbin is None or self.Gst is None:
            return
        if percent is not None:
            self.volume_percent = clamp(percent, 0, 100)
        if muted is not None:
            self.muted = muted or self.volume_percent <= 0
        if percent is not None or muted is not None:
            self._remember_volume()
        resume_time = self.smart_resume_time() if self.soft_paused else None
        self.set_output_volume(PREROLL_VOLUME_FLOOR)
        if resume_time is not None:
            self.seek_without_sync(resume_time)
        elif self.track_index_for_time(self.current_time) != self.track_index:
            self.seek_without_sync(self.current_time)
        self.soft_paused = False
        self.soft_pause_time = 0.0
        self.soft_paused_at = 0.0
        self.playbin.set_state(self.Gst.State.PLAYING)
        self.is_playing = True
        if resume_time is not None:
            time.sleep(RESUME_SETTLE_SECONDS)
        self.apply_audio_state()
        if sync:
            self.request_progress_sync()

    def pause(self, sync: bool = True, keep_audio_alive: bool = True) -> None:
        if self.playbin is None or self.Gst is None:
            return
        self._interrupt_startup_probe()
        self.is_playing = False
        if keep_audio_alive:
            self.soft_paused = True
            self.soft_pause_time = self.current_time
            self.soft_paused_at = time.monotonic()
            self.apply_audio_state()
            if sync:
                self.request_progress_sync()
            return
        self.soft_paused = False
        self.soft_pause_time = 0.0
        self.soft_paused_at = 0.0
        self.playbin.set_state(self.Gst.State.PAUSED)
        if sync:
            self.request_progress_sync()

    def seek_global(self, current_time: float) -> bool:
        if self.session is None or self.playbin is None or self.Gst is None:
            return False
        target = min(max(current_time, 0), self.session.duration)
        new_index = self.track_index_for_time(target)
        if new_index != self.track_index:
            was_playing = self.is_playing
            self.track_index = new_index
            self.current_time = target
            self.load_current_track()
            self.playbin.set_state(self.Gst.State.PLAYING if was_playing else self.Gst.State.PAUSED)
        elif not self.seek_track(target - self.current_track.start_offset):
            return False
        self.current_time = target
        if self.soft_paused:
            self.soft_pause_time = target
            self.soft_paused_at = time.monotonic()
        # A deliberate seek is a new edit, even if another device moved the book.
        self._session_progress = SessionProgress(None)
        self.request_progress_sync()
        return True

    def seek_without_sync(self, current_time: float) -> bool:
        if self.session is None or self.playbin is None or self.Gst is None:
            return False
        self.current_time = min(max(current_time, 0), self.session.duration)
        new_index = self.track_index_for_time(self.current_time)
        if new_index != self.track_index:
            self.track_index = new_index
            self.load_current_track()
            return True
        return self.seek_track(self.current_time - self.current_track.start_offset)

    def mark_finished(self) -> bool:
        if self.session is None or self.session.duration <= 0:
            return False
        self.pause(sync=False)
        self.current_time = self.session.duration
        self._session_progress = SessionProgress(None)
        self.request_progress_sync()
        self.stop()
        return True

    def smart_resume_time(self) -> float:
        rewind = smart_resume_rewind_seconds(time.monotonic() - self.soft_paused_at)
        return max(0.0, self.soft_pause_time - rewind)

    def _apply_progress_handoff(self) -> bool:
        progress = self._session_progress
        if progress is None or progress.taken_over is None or self.session is None:
            return False
        checkpoint = progress.taken_over
        self.pause(sync=False, keep_audio_alive=False)
        if not self.seek_without_sync(checkpoint[0]):
            self.release_pipeline()
            raise PlaybackError("Reopen the book to use the position saved on Audiobookshelf.")
        self.session.progress_updated_at = checkpoint[1]
        self._last_saved_position = self.current_time
        self._session_progress = SessionProgress(checkpoint)
        return True

    def poll(self) -> None:
        if self.playbin is None or self.Gst is None:
            return
        if self._apply_progress_handoff() or not self.is_playing:
            return
        if (probe := self._startup_probe) is not None:
            probe.poll()
            if probe.timing.status is not None:
                self._startup_probe = None
        self.handle_bus_messages()
        if self.playbin is None or self.session is None or not self.is_playing:
            return
        success, position = self.playbin.query_position(self.Gst.Format.TIME)
        if success and self.session is not None:
            self.current_time = self.current_track.start_offset + position / self.Gst.SECOND
        if self.sleep_boundary is not None and self.current_time >= self.sleep_boundary:
            self.pause(sync=False, keep_audio_alive=False)
            return
        if self.is_playing and time.monotonic() - self.last_sync >= PROGRESS_SYNC_SECONDS:
            self.request_progress_sync()
            self.last_sync = time.monotonic()

    @property
    def current_track(self):
        if self.session is None:
            raise PlaybackError("No playback session is active.")
        return self.session.tracks[self.track_index]

    def track_index_for_time(self, current_time: float) -> int:
        if self.session is None:
            return 0
        for index, track in enumerate(self.session.tracks):
            if track.start_offset <= current_time < track.start_offset + track.duration:
                return index
        return max(0, len(self.session.tracks) - 1)

    def load_current_track(
        self,
        apply_audio: bool = True,
        *,
        skip_zero_seek: bool = False,
        seek_during_preroll: bool = False,
        cancelled: Callable[[], bool] | None = None,
        timing: PlaybackTiming | None = None,
    ) -> None:
        if self.playbin is None or self.Gst is None:
            return
        self._interrupt_startup_probe()
        track = self.current_track
        seek_seconds = max(0, self.current_time - track.start_offset)
        if timing is not None and isinstance(self.session, PlaybackSession):
            timing.record_track(self.session, self.track_index, self.current_time, seek_seconds)
        if isinstance(self.client, AudiobookshelfClient) and not track.url.startswith("file:"):
            try:
                with measure_pipeline_setup(timing, "media_url"):
                    track.url = self.client.authenticated_media_url(track.url)
            except AudiobookshelfError as error:
                raise PlaybackError(str(error)) from error
        with measure_pipeline_setup(timing, "reset_to_null"):
            self.playbin.set_state(self.Gst.State.NULL)
        with measure_pipeline_setup(timing, "audio_sink_setup"):
            self.configure_audio_sink()
        with measure_pipeline_setup(timing, "set_uri"):
            self.playbin.set_property("uri", track.url)
        if apply_audio:
            self.apply_audio_state()
        resume_seconds = seek_seconds if seek_during_preroll and seek_seconds > 0 else None
        preroll_wait = self._preroll_current_track(timing, resume_seconds, cancelled)
        if preroll_wait == "cancelled" or (cancelled is not None and cancelled()):
            return
        if (
            skip_zero_seek
            and self.current_time == 0
            and seek_seconds == 0
            and self.playback_rate == 1.0
        ):
            if timing is not None:
                timing.seek_wait = "skipped"
            return

        if preroll_wait != "seek_started":
            if timing is not None:
                timing.seek_mode = "after_preroll"
                timing.seek_at = timing.clock()
            self.seek_track(seek_seconds)
        seek_wait = self._wait_during_start(cancelled)
        if timing is not None:
            timing.seek_finished_at = timing.clock()
            timing.seek_wait = seek_wait

    def _preroll_current_track(
        self,
        timing: PlaybackTiming | None,
        resume_seconds: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> str:
        probe = None
        if timing is not None:
            with measure_pipeline_setup(timing, "preroll_probe_setup"):
                probe = PrerollProbe(
                    self.Gst,
                    self.playbin,
                    timing.clock,
                    trace_source_reads=os.environ.get("CHAPTER_TRACE_SOURCE_READS") == "1",
                )
                probe.attach()
            timing.preroll_at = timing.clock()
        try:
            self.playbin.set_state(self.Gst.State.PAUSED)
            if timing is not None:
                timing.pause_returned_at = timing.clock()
            if resume_seconds is None:
                preroll_wait = self._wait_during_start(cancelled)
            else:
                preroll_wait = wait_for_resume_seek(
                    self.wait_for_state_change,
                    lambda: self._try_resume_seek(resume_seconds, timing, cancelled),
                    clock=timing.clock if timing is not None else time.monotonic,
                    timeout=STATE_CHANGE_TIMEOUT_SECONDS,
                    cancelled=cancelled,
                )
            if timing is not None:
                timing.preroll_finished_at = (
                    timing.seek_at if preroll_wait == "seek_started" else timing.clock()
                )
                timing.preroll_wait = preroll_wait
            return preroll_wait
        finally:
            if probe is not None and timing is not None:
                end = timing.preroll_finished_at
                timing.preroll_milestones = probe.finish(end if end is not None else timing.clock())

    def _try_resume_seek(
        self,
        seconds: float,
        timing: PlaybackTiming | None,
        cancelled: Callable[[], bool] | None,
    ) -> bool | None:
        if not can_seek_to(self.Gst, self.playbin, seconds):
            return None
        if cancelled is not None and cancelled():
            return None
        if timing is not None:
            timing.early_seek_attempt_at = timing.clock()
        try:
            accepted = self.seek_track(seconds, report_failure=False)
        except Exception:
            return False
        if accepted and timing is not None:
            timing.seek_mode = "early"
            timing.seek_at = timing.early_seek_attempt_at
        return accepted

    def seek_track(self, seconds: float, *, rate: float | None = None, report_failure: bool = True) -> bool:
        if self.playbin is None or self.Gst is None:
            return False
        if rate is None:
            rate = self.playback_rate
        if abs(rate - 1.0) >= 0.001:
            ok = self.playbin.seek(
                rate,
                self.Gst.Format.TIME,
                self.Gst.SeekFlags.FLUSH | self.Gst.SeekFlags.KEY_UNIT,
                self.Gst.SeekType.SET,
                int(seconds * self.Gst.SECOND),
                self.Gst.SeekType.NONE,
                -1,  # seek() takes a signed gint64. Gst.CLOCK_TIME_NONE is unsigned.
            )
            if ok is False and report_failure:
                print(f"GStreamer rate seek failed at {seconds:.1f}s and {rate:g}x")
            return bool(ok)
        ok = self.playbin.seek_simple(
            self.Gst.Format.TIME,
            self.Gst.SeekFlags.FLUSH | self.Gst.SeekFlags.KEY_UNIT,
            int(seconds * self.Gst.SECOND),
        )
        if ok is False and report_failure:
            print(f"GStreamer seek failed at {seconds:.1f}s")
        return bool(ok)

    def set_playback_rate(self, rate: float) -> None:
        try:
            requested_rate = float(rate)
        except (TypeError, ValueError) as error:
            raise PlaybackError("Playback speed must be a number.") from error
        if not math.isfinite(requested_rate) or requested_rate <= 0:
            raise PlaybackError("Playback speed must be greater than zero.")
        if abs(self.playback_rate - requested_rate) < 0.001:
            return

        if self.playbin is not None and self.Gst is not None and self.session is not None:
            track_start = self.current_track.start_offset
            current_time = self.current_time
            try:
                if self.is_playing:
                    success, position = self.playbin.query_position(self.Gst.Format.TIME)
                    if success and 0 <= position < self.Gst.CLOCK_TIME_NONE:
                        current_time = track_start + position / self.Gst.SECOND
                accepted = self.seek_track(max(0.0, current_time - track_start), rate=requested_rate)
            except Exception as error:
                raise PlaybackError(f"Could not change playback speed: {error}") from error
            if not accepted:
                raise PlaybackError("Could not change playback speed.")
            self.current_time = current_time
        self.playback_rate = requested_rate

    def configure_tempo_filter(self) -> None:
        if self.playbin is None or self.Gst is None:
            return
        try:
            tempo_filter = self.Gst.ElementFactory.make("scaletempo", "chapter-tempo")
            if tempo_filter is None:
                print("GStreamer scaletempo is unavailable; playback pitch may change with speed.")
                return
            self.playbin.set_property("audio-filter", tempo_filter)
        except Exception as error:
            print(f"GStreamer tempo filter could not be configured: {error}")

    def _wait_during_start(self, cancelled: Callable[[], bool] | None) -> str:
        if cancelled is None:
            return self.wait_for_state_change()
        return wait_for_resume_seek(
            self.wait_for_state_change, None, clock=time.monotonic,
            timeout=STATE_CHANGE_TIMEOUT_SECONDS, cancelled=cancelled,
        )

    def wait_for_state_change(self, timeout: float = STATE_CHANGE_TIMEOUT_SECONDS) -> str:
        if self.playbin is None or self.Gst is None or not hasattr(self.playbin, "get_state"):
            return "unavailable"
        try:
            result, _, _ = self.playbin.get_state(
                int(timeout * self.Gst.SECOND)
            )
        except Exception as error:
            print(f"GStreamer state wait failed: {error}")
            return "error"
        return {
            self.Gst.StateChangeReturn.SUCCESS: "completed",
            self.Gst.StateChangeReturn.ASYNC: "timeout",
            self.Gst.StateChangeReturn.FAILURE: "failed",
            self.Gst.StateChangeReturn.NO_PREROLL: "no_preroll",
        }.get(result, "unknown")

    def configure_audio_sink(self) -> None:
        if self.playbin is None or self.Gst is None:
            return

        if not self.bluetooth_output_enabled:
            self.select_default_audio_sink()
            return

        device = preferred_bluetooth_alsa_device(self.bluetooth_address)
        if not device:
            self.select_default_audio_sink()
            return

        try:
            sink = self.Gst.ElementFactory.make("alsasink", "chapter-bluetooth-sink")
            if sink is None:
                print("GStreamer alsasink is unavailable; using default speaker output.")
                self.select_default_audio_sink()
                return
            sink.set_property("device", device)
            self.playbin.set_property("audio-sink", sink)
            self.audio_output_device = device
            self.select_volume_output("bluetooth")
            print(f"Bluetooth audio output selected: {device}")
        except Exception as error:
            self.select_default_audio_sink()
            print(f"Bluetooth audio output could not be selected: {error}")

    def select_default_audio_sink(self) -> None:
        self.audio_output_device = ""
        self.select_volume_output("speaker")
        if self.playbin is None:
            return
        try:
            self.playbin.set_property("audio-sink", None)
        except Exception as error:
            print(f"GStreamer default audio sink could not be selected: {error}")

    def set_bluetooth_output_enabled(self, enabled: bool, address: str = "") -> None:
        self.bluetooth_output_enabled = enabled
        if address or not enabled:
            self.bluetooth_address = address
        if self.playbin is None:
            self.select_volume_output("bluetooth" if enabled else "speaker")

    def refresh_audio_output(self) -> None:
        if self.session is None or self.playbin is None or self.Gst is None:
            return
        was_playing = self.is_playing
        self.set_output_volume(PREROLL_VOLUME_FLOOR)
        self.load_current_track(apply_audio=False)
        self.playbin.set_state(self.Gst.State.PLAYING if was_playing else self.Gst.State.PAUSED)
        if was_playing:
            time.sleep(RESUME_SETTLE_SECONDS)
        self.apply_audio_state()

    def advance_within_session(self) -> bool:
        if self.session is None or self.playbin is None or self.Gst is None:
            return False
        next_index = self.track_index + 1
        last_track = next_index >= len(self.session.tracks)
        track_end = self.session.duration if last_track else self.session.tracks[next_index].start_offset
        if self.sleep_boundary is not None and track_end >= self.sleep_boundary:
            self.current_time = self.sleep_boundary
            self.pause(sync=False, keep_audio_alive=False)
            return False
        if last_track:
            finished = self.session
            self.current_time = finished.duration
            self.request_progress_sync()
            self.is_playing = False
            self.stop()
            self.completed_session = finished
            return False
        self.track_index += 1
        self.current_time = self.current_track.start_offset
        self.load_current_track()
        self.playbin.set_state(self.Gst.State.PLAYING)
        self.is_playing = True
        return True

    def handle_bus_messages(self) -> None:
        if self.playbin is None or self.Gst is None:
            return
        bus = self.playbin.get_bus()
        while True:
            message = bus.timed_pop_filtered(0, self.Gst.MessageType.ERROR | self.Gst.MessageType.EOS)
            if message is None:
                return
            if message.type == self.Gst.MessageType.EOS:
                if not self.advance_within_session():
                    return
            elif message.type == self.Gst.MessageType.ERROR:
                self.stop()
                raise PlaybackError("GStreamer playback error.")

    def sync_progress(self) -> None:
        if self.client is None or self.session is None:
            return
        try:
            self.client.update_progress(
                self.session.library_item_id,
                self.current_time,
                self.session.duration,
                session_id=self.session.id,
                time_listened=self.time_listened_since_last_sync(),
            )
            self.last_sync = time.monotonic()
        except AudiobookshelfError as error:
            print(f"Progress sync failed: {error}")

    def request_progress_sync(self) -> None:
        if self.client is None or self.session is None:
            return
        client = self.client
        session_id = self.session.id
        item_id = self.session.library_item_id
        current_time = self.current_time
        duration = self.session.duration
        time_listened = self.time_listened_since_last_sync()
        if isinstance(client, PodcastProgressClient) and client.account:
            try:
                client.update_progress(item_id, current_time, duration, session_id, time_listened)
            except OSError as error:
                print(f"Could not save episode progress: {type(error).__name__}")
            self.last_sync = time.monotonic()
            return
        account = getattr(client, "progress_account", "")
        if self.progress_sync is not None and isinstance(account, str) and account:
            self.progress_sync.record(
                account, item_id, current_time, duration, self.session.episode_id, session_id, time_listened,
                after=self.session.progress_updated_at, session=self._session_progress,
            )
            self._last_saved_position = current_time
            self.last_sync = time.monotonic()
            return
        with self._sync_lock:
            if self._sync_in_flight:
                return
            self._sync_in_flight = True
            self.last_sync = time.monotonic()

        def run_sync() -> None:
            try:
                client.update_progress(
                    item_id,
                    current_time,
                    duration,
                    session_id=session_id,
                    time_listened=time_listened,
                )
            except Exception as error:
                print(f"Progress sync failed: {error}")
            finally:
                with self._sync_lock:
                    self._sync_in_flight = False

        threading.Thread(target=run_sync, daemon=True).start()

    def time_listened_since_last_sync(self) -> float:
        if not self.is_playing:
            return 0.0
        return max(0.0, time.monotonic() - self.last_sync)

    def configure_output_volumes(self, profiles: OutputVolumes) -> None:
        self.output_volumes = profiles
        self.select_volume_output(self.volume_output)

    def select_volume_output(self, output: str) -> None:
        if self.output_volumes is not None:
            self.volume_output = output
            volume = self.output_volumes.for_output(output)
            self.volume_percent = volume.percent
            self.muted = volume.muted or volume.percent == 0

    def _remember_volume(self) -> None:
        if self.output_volumes is not None:
            self.output_volumes = self.output_volumes.with_level(
                self.volume_output, VolumeState(self.volume_percent, self.muted),
            )

    def set_volume_for_output(self, output: str, percent: int, muted: bool) -> None:
        if self.output_volumes is None or output == self.volume_output:
            self.set_volume_state(percent, muted)
            return
        level = clamp(percent, 0, 100)
        self.output_volumes = self.output_volumes.with_level(output, VolumeState(level, muted or level == 0))

    def set_volume_state(self, percent: int, muted: bool) -> None:
        self.volume_percent = clamp(percent, 0, 100)
        self.muted = muted or self.volume_percent <= 0
        self._remember_volume()
        if self.muted:
            self._interrupt_startup_probe()
        self.apply_audio_state()

    def set_spoken_navigation_ducked(self, ducked: bool) -> None:
        if self.spoken_navigation_ducked == ducked:
            return
        self.spoken_navigation_ducked = ducked
        self.apply_audio_state()

    def set_sleep_gain(self, gain: float) -> None:
        self.sleep_gain = min(max(gain, 0.0), 1.0)
        self.set_output_volume(self.target_effective_volume())

    def set_night_volume_limit(self, percent: int) -> None:
        self.night_volume_limit = min(max(percent, 1), 100)
        self.apply_audio_state()

    def apply_audio_state(self, cancelled: Callable[[], bool] | None = None) -> None:
        if self.playbin is None:
            return
        target = self.target_effective_volume()
        with self._audio_lock:
            if self.playbin is None:
                return
            self.fade_volume(target, cancelled)

    def set_output_volume(self, value: float) -> None:
        if self.playbin is None:
            return
        value = min(max(value, 0.0), MAX_EFFECTIVE_VOLUME)
        self.playbin.set_property("volume", value)
        self._effective_volume = value
        if value > PREROLL_VOLUME_FLOOR and (probe := self._startup_probe) is not None:
            probe.gain_applied()
            if probe.timing.status is not None:
                self._startup_probe = None

    def _interrupt_startup_probe(self) -> None:
        if (probe := self._startup_probe) is not None:
            self._startup_probe = None
            probe.timing.observe_output("output_interrupted")

    def target_effective_volume(self) -> float:
        if self.muted or self.soft_paused:
            return SILENCE_VOLUME
        target = (min(self.volume_percent, self.night_volume_limit) / 100) * MAX_EFFECTIVE_VOLUME * self.sleep_gain
        if self.spoken_navigation_ducked:
            target *= SPOKEN_NAVIGATION_DUCK_FACTOR
        return target

    def fade_volume(self, target: float, cancelled: Callable[[], bool] | None = None) -> None:
        target = min(max(target, 0.0), MAX_EFFECTIVE_VOLUME)
        start = self._effective_volume
        if cancelled and cancelled():
            return
        if abs(start - target) < 0.01:
            self.set_output_volume(target)
            return

        delay = VOLUME_FADE_SECONDS / VOLUME_FADE_STEPS
        for step in range(1, VOLUME_FADE_STEPS + 1):
            if cancelled and cancelled():
                self.set_output_volume(SILENCE_VOLUME)
                return
            value = start + (target - start) * (step / VOLUME_FADE_STEPS)
            self.set_output_volume(value)
            time.sleep(delay)
        self._effective_volume = target


def clamp(value: int, minimum: int, maximum: int) -> int:
    return min(max(value, minimum), maximum)


def smart_resume_rewind_seconds(paused_seconds: float) -> float:
    if paused_seconds < SMART_RESUME_SHORT_PAUSE_SECONDS:
        return 0.0
    if paused_seconds < SMART_RESUME_LONG_PAUSE_SECONDS:
        return SMART_RESUME_MEDIUM_REWIND_SECONDS
    return SMART_RESUME_LONG_REWIND_SECONDS
