"""Serialize playback operations and apply only the current book request."""

from __future__ import annotations

import queue
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from typing import Callable, TypeVar
from uuid import uuid4

from .display_frames import DisplayFrame
from .models import PlaybackSession
from .playback import GStreamerPlayback, PlaybackError
from .playback_start import (
    BOOK_PLAYBACK_ERROR_FRAME,
    PlaybackPreparation,
    PreparedPlayback,
    prepare_book_request,
)
from .playback_startup_history import StartupSample
from .playback_timing import PlaybackTiming
from .player_control import WebPlayerCommand, save_command_result
from .player_state import OutputVolumes
from .rotary_ui import MenuCommand


_Result = TypeVar("_Result")


@dataclass(frozen=True)
class PlaybackSnapshot:
    session: PlaybackSession | None = None
    current_time: float = 0
    is_playing: bool = False
    audio_output_device: str = ""
    volume_output: str = "speaker"
    output_volumes: OutputVolumes | None = None
    session_key: str = ""
    completed_session: PlaybackSession | None = None
    playback_rate: float = 1.0


@dataclass(frozen=True)
class PlaybackStartResult:
    session: PlaybackSession | None = None
    error_frame: DisplayFrame | None = None


@dataclass(frozen=True)
class _BookRequest:
    start_over: bool
    timing: PlaybackTiming
    key: str
    web_command: WebPlayerCommand | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)


class PlaybackOwner:
    """Give the OLED service snapshots of playback while one thread owns the engine."""

    def __init__(
        self, engine: GStreamerPlayback, *, startup_reporter: Callable[[StartupSample], None] | None = None,
    ) -> None:
        self._engine = engine
        self._startup_reporter = startup_reporter
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chapter-playback")
        self._prepared: queue.SimpleQueue[tuple[_BookRequest, PlaybackPreparation]] = (
            queue.SimpleQueue()
        )
        self._request_lock = threading.Lock()
        self._book_request: _BookRequest | None = None
        self._starting_book: Future[PlaybackStartResult | None] | None = None
        self._polling: Future[None] | None = None
        self._engine_session: PlaybackSession | None = None
        self._snapshot = PlaybackSnapshot()
        self._paused_for_mute = False
        self._closed = False
        self._publish_snapshot()

    @property
    def state(self) -> PlaybackSnapshot:
        return self._snapshot

    @property
    def session(self) -> PlaybackSession | None:
        return self.state.session

    @property
    def current_time(self) -> float:
        return self.state.current_time

    @property
    def is_playing(self) -> bool:
        return self.state.is_playing

    @property
    def loading_book(self) -> bool:
        return self._book_request is not None

    @property
    def opening_key(self) -> str:
        request = self._book_request
        return request.key if request else ""

    @property
    def starting_book(self) -> bool:
        return self._starting_book is not None

    def request_book(self, command: MenuCommand) -> None:
        requested_at = time.monotonic()
        self.cancel_book_request()
        self._polling = None
        timing = PlaybackTiming(
            selected_at=command.selected_at if command.selected_at is not None else requested_at,
            requested_at=requested_at,
            action="start" if command.start_over else "continue",
            startup_reporter=self._startup_reporter,
        )
        request = _BookRequest(
            start_over=command.start_over, timing=timing, web_command=command.web_command,
            key=command.web_command.id if command.web_command and command.web_command.id else uuid4().hex,
        )
        with self._request_lock:
            if self._closed:
                raise RuntimeError("Playback owner is closed")
            self._book_request = request

        def prepare() -> None:
            try:
                result = prepare_book_request(command, timing=timing)
            except Exception:
                print("Playback preparation failed:")
                traceback.print_exc()
                result = PlaybackPreparation(error_frame=BOOK_PLAYBACK_ERROR_FRAME)
            timing.prepared_at = timing.clock()
            if not request.cancelled.is_set():
                self._prepared.put((request, result))

        try:
            threading.Thread(target=prepare, name="chapter-book-request", daemon=True).start()
        except RuntimeError:
            timing.prepared_at = timing.clock()
            self._prepared.put(
                (request, PlaybackPreparation(error_frame=BOOK_PLAYBACK_ERROR_FRAME))
            )

    def cancel_book_request(self) -> None:
        self._finish_book_request("superseded")

    def cancel_opening(self, key: str) -> bool:
        if not key or self.opening_key != key:
            return False
        self._finish_book_request("cancelled")
        return True

    def _finish_book_request(self, outcome: str) -> None:
        with self._request_lock:
            request = self._book_request
            if request is not None:
                request.cancelled.set()
            self._book_request = None
            self._starting_book = None
        if request is not None and request.web_command is not None:
            save_command_result(request.web_command, outcome)

    def advance_book_request(self) -> PlaybackStartResult | None:
        """Consume results after handling new OLED and web commands for this tick."""
        while True:
            try:
                request, result = self._prepared.get_nowait()
            except queue.Empty:
                break
            if request is not self._book_request:
                continue
            if result.prepared is None:
                request.timing.observe_output("preparation_failed")
                request.timing.report()
                self._finish_book_request("rejected")
                return PlaybackStartResult(error_frame=result.error_frame)
            self._starting_book = self._submit(partial(self._start_book, request, result.prepared))

        if self._starting_book is None or not self._starting_book.done():
            return None
        try:
            result = self._starting_book.result()
        except Exception:
            print("Could not finish opening the book:")
            traceback.print_exc()
            result = PlaybackStartResult(error_frame=BOOK_PLAYBACK_ERROR_FRAME)
        self._finish_book_request("applied" if result and result.session else "rejected")
        return result

    def _start_book(
        self, request: _BookRequest, prepared: PreparedPlayback
    ) -> PlaybackStartResult | None:
        if request.cancelled.is_set():
            return None
        timing = request.timing
        timing.pipeline_at = timing.clock()
        try:
            self._engine.start(
                prepared.session,
                prepared.client,
                start_over=request.start_over,
                cancelled=request.cancelled.is_set,
                timing=timing,
            )
        except PlaybackError as error:
            print(f"Playback failed: {error}")
        except Exception:
            print("Playback startup failed:")
            traceback.print_exc()
        else:
            result = None
            with self._request_lock:
                if not self._closed and not request.cancelled.is_set():
                    timing.startup_finished_at = timing.clock()
                    self._paused_for_mute = False
                    self._publish_snapshot(session_key=request.key)
                    result = PlaybackStartResult(session=self.session)
            if result is not None:
                if request.start_over and self._engine.progress_sync is not None:
                    try:
                        self._engine.request_progress_sync()
                    except OSError as error:
                        print(f"Could not save the starting position: {error}")
                timing.report()
                return result
            self._engine.release_pipeline()
            return None

        try:
            if not request.cancelled.is_set():
                timing.startup_finished_at = timing.clock()
                timing.report_startup_failure()
        finally:
            self._engine.release_pipeline()
        return PlaybackStartResult(error_frame=BOOK_PLAYBACK_ERROR_FRAME)

    def pause(self, sync: bool = True) -> bool:
        def pause_if_playing() -> bool:
            self._paused_for_mute = False
            if not self._engine.is_playing:
                return False
            self._engine.pause(sync=sync)
            return True

        return self._submit(pause_if_playing).result()

    def resume(
        self, sync: bool = False, percent: int | None = None, muted: bool | None = None,
        *, selection_key: str = "",
    ) -> bool:
        def resume_if_paused() -> bool:
            self._paused_for_mute = False
            if self._engine.session is None:
                return False
            if not self._engine.is_playing:
                self._engine.resume(sync=sync, percent=percent, muted=muted)
            elif not selection_key:
                return False
            if selection_key:
                self._publish_snapshot(session_key=selection_key)
            return self._engine.is_playing

        return self._submit(resume_if_paused).result()

    def toggle(self) -> None:
        def toggle() -> None:
            self._paused_for_mute = False
            self._engine.toggle()

        self._submit(toggle).result()

    def seek_global(self, current_time: float) -> bool:
        return self._submit(partial(self._engine.seek_global, current_time)).result()

    def mark_finished(self) -> bool:
        return self._submit(self._engine.mark_finished).result()

    def sleep_at_position(self, rewind_to: float | None) -> None:
        def sleep() -> None:
            self._engine.sleep_boundary = None
            self._engine.pause(sync=rewind_to is None, keep_audio_alive=False)
            if rewind_to is not None:
                self._engine.seek_global(rewind_to)

        self._submit(sleep).result()

    def set_sleep_boundary(self, position: float | None) -> None:
        self._post(lambda: setattr(self._engine, "sleep_boundary", position), "sleep boundary")

    def save_bookmark(self) -> Future[bool]:
        def snapshot():
            session = self._engine.session
            add_bookmark = getattr(self._engine.client, "create_bookmark", None)
            if session is None or session.episode_id or not callable(add_bookmark):
                return None
            return add_bookmark, session.library_item_id, self._engine.current_time

        pending: Future[bool] = Future()
        payload = self._submit(snapshot).result()
        if payload is None:
            pending.set_result(False)
            return pending
        add_bookmark, item_id, position = payload

        def send() -> None:
            try:
                add_bookmark(item_id, position, f"Chapter · {int(position) // 60}:{int(position) % 60:02d}")
            except (OSError, RuntimeError, ValueError):
                pending.set_result(False)
            else:
                pending.set_result(True)

        threading.Thread(target=send, daemon=True).start()
        return pending

    def poll(self) -> None:
        if self._polling is not None:
            if not self._polling.done():
                return
            future = self._polling
            self._polling = None
            future.result()
        if self.session is not None:
            self._polling = self._submit(self._engine.poll)

    def set_playback_rate(self, rate: float) -> None:
        self._submit(partial(self._engine.set_playback_rate, rate)).result()

    def set_session_playback_rate(self, session_key: str, rate: float) -> Future[bool]:
        def change_rate() -> bool:
            if self.session is None or self.state.session_key != session_key or self.loading_book:
                return False
            try:
                self._engine.set_playback_rate(rate)
            except PlaybackError as error:
                print(f"Playback speed update failed: {error}")
                return False
            return True

        return self._submit(change_rate)

    def configure_output_volumes(self, profiles: OutputVolumes) -> None:
        self._submit(partial(self._engine.configure_output_volumes, profiles)).result()

    def set_volume_state(self, percent: int, muted: bool, *, output: str | None = None) -> Future[None]:
        action = (
            partial(self._engine.set_volume_for_output, output, percent, muted)
            if output is not None else partial(self._engine.set_volume_state, percent, muted)
        )
        return self._post(action, "volume update")

    def pause_for_mute(self, *, sync: bool = False, percent: int | None = None) -> Future[None]:
        def pause() -> None:
            if percent is not None:
                self._engine.set_volume_state(percent, True)
            self._paused_for_mute |= self._engine.is_playing
            self._engine.pause(sync=sync)

        return self._post(pause, "mute pause")

    def resume_after_mute(self, percent: int, muted: bool) -> Future[None]:
        def resume() -> None:
            if self._paused_for_mute and not self._engine.is_playing:
                self._engine.resume(sync=False, percent=percent, muted=muted)
            else:
                self._engine.set_volume_state(percent, muted)
            self._paused_for_mute = False

        return self._post(resume, "mute resume")

    def clear_mute_pause(self) -> None:
        def clear() -> None:
            self._paused_for_mute = False

        self._post(clear, "reset mute pause")

    def set_spoken_navigation_ducked(self, ducked: bool) -> None:
        self._post(partial(self._engine.set_spoken_navigation_ducked, ducked), "speech ducking")

    def set_sleep_gain(self, gain: float) -> None:
        self._post(partial(self._engine.set_sleep_gain, gain), "sleep fade")

    def set_night_volume_limit(self, percent: int) -> None:
        self._post(partial(self._engine.set_night_volume_limit, percent), "night volume limit")

    def set_bluetooth_output_enabled(self, enabled: bool, address: str = "") -> None:
        self._post(partial(self._engine.set_bluetooth_output_enabled, enabled, address), "Bluetooth routing")

    def refresh_audio_output(self) -> None:
        self._post(self._engine.refresh_audio_output, "Bluetooth audio output refresh")

    def stop(self) -> None:
        self.cancel_book_request()
        self._polling = None
        self._submit(self._stop_engine).result()

    def _stop_engine(self) -> None:
        self._paused_for_mute = False
        self._engine.stop()

    def close(self) -> None:
        with self._request_lock:
            if self._closed:
                return
            self._closed = True
        with self._worker:
            try:
                self.cancel_book_request()
            finally:
                # Jobs accepted before close run first. The final engine operation releases audio.
                self._worker.submit(self._run_on_engine, self._stop_engine).result()

    def stop_listening(self) -> float | None:
        def stop() -> float | None:
            self._paused_for_mute = False
            return self._engine.stop_listening()

        return self._submit(stop).result()

    def _publish_snapshot(self, session_key: str = "") -> None:
        session = self._engine.session
        display_session = self._snapshot.session
        if session is not self._engine_session:
            # The engine can rewrite track URLs. The menu keeps its own session data.
            display_session = deepcopy(session)
            self._engine_session = session
            session_key = (session_key or uuid4().hex) if session is not None else ""
        else:
            session_key = session_key or self._snapshot.session_key
        self._snapshot = PlaybackSnapshot(
            session=display_session,
            current_time=self._engine.current_time,
            is_playing=self._engine.is_playing,
            audio_output_device=self._engine.audio_output_device,
            volume_output=self._engine.volume_output,
            output_volumes=self._engine.output_volumes,
            session_key=session_key,
            completed_session=getattr(self._engine, "completed_session", None),
            playback_rate=getattr(self._engine, "playback_rate", 1.0),
        )

    def _post(self, action: Callable[[], None], label: str) -> Future[None]:
        def run() -> None:
            try:
                action()
            except Exception:
                print(f"Playback {label} failed:")
                traceback.print_exc()
                raise

        return self._submit(run)

    def _submit(self, action: Callable[[], _Result]) -> Future[_Result]:
        with self._request_lock:
            if self._closed:
                raise RuntimeError("Playback owner is closed")
            return self._worker.submit(self._run_on_engine, action)

    def _run_on_engine(self, action: Callable[[], _Result]) -> _Result:
        try:
            return action()
        finally:
            self._publish_snapshot()
