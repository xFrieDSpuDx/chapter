from __future__ import annotations

import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

from abs_kids_player import oled_service
from abs_kids_player.appliance_io import InputEvent
from abs_kids_player.catalogue_settings import catalogue_request_key, catalogue_status_key
from abs_kids_player.config import AppConfig
from abs_kids_player.models import Book, PlaybackSession
from abs_kids_player.oled_input import handle_oled_input_events
from abs_kids_player.oled_service import (
    BOOK_LOADING_FRAME,
    handle_menu_command,
    handle_web_book_request,
    handle_web_player_commands,
)
from abs_kids_player.playback import GStreamerPlayback, PlaybackError
from abs_kids_player.playback_owner import PlaybackOwner, PlaybackStartResult
from abs_kids_player.playback_start import (
    BOOK_PLAYBACK_ERROR_FRAME,
    PlaybackPreparation,
    PreparedPlayback,
)
from abs_kids_player.player_control import WebPlayerCommand
from abs_kids_player.progress_sync import progress_account
from abs_kids_player.rotary_ui import ApplianceMenu, CommandType, MenuCommand, Screen
from abs_kids_player.software_update import SoftwareUpdateState
from abs_kids_player.wifi import WifiStatus


class RecordingEngine(GStreamerPlayback):
    def __init__(self) -> None:
        super().__init__()
        self.started_books: list[str] = []
        self.before_start = Mock()
        self.operations: list[tuple[str, threading.Thread]] = []

    def start(self, session, client, *, start_over=False, cancelled=None, timing=None) -> None:
        self.record("start")
        self.stop()
        self.before_start(session.library_item_id)
        if cancelled is not None and cancelled():
            return
        self.session = session
        self.current_time = session.current_time
        self.is_playing = True
        self.started_books.append(session.library_item_id)

    def set_volume_state(self, percent: int, muted: bool) -> None:
        self.record("volume")
        super().set_volume_state(percent, muted)

    def pause(self, sync: bool = True, keep_audio_alive: bool = True) -> None:
        self.record("pause")
        self.is_playing = False

    def resume(self, sync: bool = False, percent=None, muted=None) -> None:
        self.record("resume")
        self.is_playing = True
        self.set_volume_state(percent if percent is not None else self.volume_percent, bool(muted))

    def refresh_audio_output(self) -> None:
        self.record("output")

    def stop(self) -> None:
        self.record("stop")
        super().stop()

    def record(self, operation: str) -> None:
        self.operations.append((operation, threading.current_thread()))


def prepare_book(book_id: str) -> PlaybackPreparation:
    session = PlaybackSession(
        id=f"session-{book_id}",
        library_item_id=book_id,
        title=book_id,
        author="",
        duration=180,
        current_time=0,
        cover_url="",
        tracks=[],
        chapters=[],
    )
    return PlaybackPreparation(prepared=PreparedPlayback(session, Mock()))


class PlaybackOwnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.menu = ApplianceMenu()
        self.engine = RecordingEngine()
        self.owner = PlaybackOwner(self.engine)
        self.addCleanup(self.owner.close)
        self.preparation_threads: dict[str, threading.Thread] = {}
        self.prepare = Mock(side_effect=prepare_book)
        self.enterContext(
            patch(
                "abs_kids_player.playback_owner.prepare_book_request",
                side_effect=self.prepare_request,
            )
        )

    def test_speed_changes_only_apply_to_the_requested_session(self):
        self.choose("first")
        self.wait_for_result()
        first = self.owner.state.session_key
        self.choose("second")
        self.wait_for_result()
        self.assertFalse(self.owner.set_session_playback_rate(first, 2).result(timeout=2))
        self.assertEqual(self.owner.state.playback_rate, 1)
        self.assertTrue(self.owner.set_session_playback_rate(self.owner.state.session_key, 1.5).result(timeout=2))
        self.assertEqual(self.owner.state.playback_rate, 1.5)

    def prepare_request(self, command: MenuCommand, *, timing=None) -> PlaybackPreparation:
        self.preparation_threads[command.book_id] = threading.current_thread()
        return self.prepare(command.book_id)

    def choose(self, book_id: str) -> None:
        frame = handle_menu_command(
            MenuCommand(CommandType.PLAY_BOOK, book_id=book_id), self.menu, self.owner
        )
        self.assertEqual(frame, BOOK_LOADING_FRAME)

    def hold_preparation(
        self, book_id: str, result: PlaybackPreparation
    ) -> tuple[threading.Event, threading.Event]:
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def load(requested_id: str) -> PlaybackPreparation:
            if requested_id != book_id:
                return prepare_book(requested_id)
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("Test did not release the book request")
            return result

        self.prepare.side_effect = load
        return entered, release

    def finish_preparation(self, book_id: str, release: threading.Event) -> None:
        release.set()
        self.preparation_threads[book_id].join(timeout=2)
        self.assertFalse(self.preparation_threads[book_id].is_alive())

    def wait_for_result(self) -> PlaybackStartResult:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = self.owner.advance_book_request()
            if result is not None:
                return result
            time.sleep(0.001)
        self.fail("Playback request did not finish")

    def test_slow_request_cannot_replace_a_book_that_started_later(self) -> None:
        entered, release = self.hold_preparation("first", prepare_book("first"))
        self.choose("first")
        self.assertTrue(entered.wait(timeout=2))

        self.choose("second")
        self.assertEqual(self.wait_for_result().session.library_item_id, "second")
        self.finish_preparation("first", release)

        self.assertIsNone(self.owner.advance_book_request())
        self.owner.set_playback_rate(1)
        self.assertFalse(self.owner.loading_book)
        self.assertEqual(self.engine.started_books, ["second"])
        self.assertEqual(self.owner.session.library_item_id, "second")

    def test_web_book_reports_superseded_by_later_knob_choice_and_applied_after_start(self) -> None:
        config = AppConfig(server_url="https://books.example.test", token="token", library_id="lib")
        command = WebPlayerCommand(
            "play_book", id="browser", created_at=time.time(), catalogue_key=catalogue_request_key(config),
            book_id="first",
        )
        entered, release = self.hold_preparation("first", prepare_book("first"))
        with (
            patch.object(oled_service, "load_config", return_value=config),
            patch("abs_kids_player.playback_owner.save_command_result") as report,
        ):
            self.assertEqual(handle_web_book_request(command, 0, self.menu, self.owner), BOOK_LOADING_FRAME)
            self.assertTrue(entered.wait(timeout=2))
            report.assert_not_called()
            self.choose("second")
            report.assert_called_once_with(command, "superseded")
            self.assertEqual(self.wait_for_result().session.library_item_id, "second")
            self.finish_preparation("first", release)
            self.assertIsNone(self.owner.advance_book_request())
            self.assertEqual(self.engine.started_books, ["second"])

            report.reset_mock()
            self.assertEqual(handle_web_book_request(command, 0, self.menu, self.owner), BOOK_LOADING_FRAME)
            self.assertEqual(self.wait_for_result().session.library_item_id, "first")
            report.assert_called_once_with(command, "applied")

    def test_browser_command_arriving_behind_a_newer_knob_selection_is_dropped(self) -> None:
        command = WebPlayerCommand("play_book", created_at=90, book_id="old")
        with patch.object(oled_service, "save_command_result") as report:
            self.assertIsNone(handle_web_book_request(command, 92, self.menu, self.owner))
        report.assert_called_once_with(command, "superseded")
        self.prepare.assert_not_called()

    def test_oled_processes_new_selection_before_an_older_result_already_waiting(self) -> None:
        entered, release = self.hold_preparation("first", prepare_book("first"))
        engine = RecordingEngine()
        saved_books = []
        ticks = 0
        deadline = time.monotonic() + 3

        def read_input(_events, _menu, *, fallback_percent, fallback_muted, **_feedback):
            nonlocal ticks
            ticks += 1
            commands = []
            if ticks == 1:
                commands = [MenuCommand(CommandType.PLAY_BOOK, book_id="first")]
            elif ticks == 2:
                self.assertTrue(entered.wait(timeout=2))
                self.finish_preparation("first", release)
                commands = [MenuCommand(CommandType.PLAY_BOOK, book_id="second")]
            return None, fallback_percent, fallback_muted, bool(commands), commands, False, False

        def save_playback(playback: PlaybackOwner) -> bool:
            saved_books.append(playback.session.library_item_id)
            return True

        def sleep_until_done(shutdown, _seconds: float, **_options) -> None:
            if "second" in saved_books or time.monotonic() >= deadline:
                shutdown.request(0, None)
            else:
                time.sleep(0.001)

        with (
            patch.object(oled_service.signal, "signal"),
            patch.multiple(
                oled_service,
                GStreamerPlayback=Mock(return_value=engine),
                SpokenNavigationFeedback=Mock(),
                consume_web_player_commands=Mock(return_value=[]),
                enable_stack_dump_signal=Mock(),
                handle_oled_input_events=read_input,
                load_cached_books=Mock(return_value=[]),
                load_config=Mock(return_value=AppConfig()),
                load_player_state=Mock(return_value=None),
                load_saved_volume=Mock(return_value=None),
                load_update_state=Mock(return_value=SoftwareUpdateState()),
                save_web_player_status=Mock(),
                save_playback_state=save_playback,
                save_output_volumes=Mock(),
                sleep_until_shutdown=sleep_until_done,
                start_bluetooth_status_worker=Mock(),
                update_state_is_active=Mock(return_value=False),
                wifi_status=Mock(return_value=WifiStatus(connected=False)),
            ),
        ):
            oled_service.run_oled_service(
                Mock(),
                splash_seconds=0,
                nav_input_enabled=False,
                volume_input_enabled=False,
            )

        self.assertEqual(engine.started_books, ["second"])
        self.assertEqual(saved_books, ["second"])

    def test_a_stale_error_cannot_clear_a_newer_loading_request(self) -> None:
        entered, release_first = self.hold_preparation(
            "first",
            PlaybackPreparation(error_frame=BOOK_PLAYBACK_ERROR_FRAME),
        )
        load_first = self.prepare.side_effect
        second_entered = threading.Event()
        release_second = threading.Event()
        self.addCleanup(release_second.set)

        def load(book_id: str) -> PlaybackPreparation:
            if book_id == "first":
                return load_first(book_id)
            second_entered.set()
            if not release_second.wait(timeout=5):
                raise AssertionError("Test did not release the newer book")
            return prepare_book(book_id)

        self.prepare.side_effect = load
        self.choose("first")
        self.assertTrue(entered.wait(timeout=2))
        self.choose("second")
        self.assertTrue(second_entered.wait(timeout=2))
        self.finish_preparation("first", release_first)

        self.assertIsNone(self.owner.advance_book_request())
        self.assertTrue(self.owner.loading_book)
        release_second.set()
        self.assertEqual(self.wait_for_result().session.library_item_id, "second")

    def test_web_cancel_during_slow_knob_selection_keeps_previous_book(self) -> None:
        self.choose("already playing")
        self.wait_for_result()
        entered, release = self.hold_preparation("accidental", prepare_book("accidental"))
        self.choose("accidental")
        self.assertTrue(entered.wait(timeout=2))
        config = AppConfig()
        command = WebPlayerCommand("cancel_open", session_key=self.owner.opening_key,
                                   catalogue_key=catalogue_status_key(config))
        with (patch.object(oled_service, "load_config", return_value=config),
              patch.object(oled_service, "consume_web_player_commands", return_value=[command]),
              patch.object(oled_service, "save_command_result") as report):
            handle_web_player_commands(self.owner, self.menu, 50, False)
        self.finish_preparation("accidental", release)
        self.assertIsNone(self.owner.advance_book_request())
        self.assertEqual(self.engine.started_books, ["already playing"])
        self.assertTrue(self.owner.is_playing)
        report.assert_called_once_with(command, "applied")

    def test_knob_cancels_browser_start_without_writing_zero_progress(self) -> None:
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def hold_start(_book_id):
            entered.set()
            release.wait(timeout=2)
        self.engine.before_start.side_effect = hold_start
        command = WebPlayerCommand("play_book", id="a" * 32, book_id="first")
        self.engine.progress_sync = Mock()
        with patch("abs_kids_player.playback_owner.save_command_result") as report:
            self.owner.request_book(MenuCommand(CommandType.PLAY_BOOK, book_id="first",
                                                start_over=True, web_command=command))
            deadline = time.monotonic() + 2
            while not entered.is_set() and time.monotonic() < deadline:
                self.owner.advance_book_request()
                entered.wait(0.001)
            self.assertTrue(entered.is_set())
            events = queue.SimpleQueue()
            events.put(InputEvent("nav_click"))
            _, _, _, _, commands, _, _ = handle_oled_input_events(events, self.menu, opening_key=self.owner.opening_key)
            cancel, = commands
            self.assertEqual(cancel.selection_key, command.id)
            handle_menu_command(cancel, self.menu, self.owner)
            self.assertFalse(self.owner.loading_book)
            release.set()
            self.owner.set_playback_rate(1)  # Wait for the engine thread to finish discarding the start.
            self.assertIsNone(self.owner.session)
            self.assertEqual(self.engine.started_books, [])
            self.engine.progress_sync.record.assert_not_called()
            report.assert_called_once_with(command, "cancelled")

    def test_late_cancel_stops_only_the_original_selection_even_if_title_repeats(self) -> None:
        self.menu.set_books([Book("first", "First", "", 180, "")])
        self.choose("first")
        first_key = self.owner.opening_key
        self.wait_for_result()
        self.assertEqual(self.owner.state.session_key, first_key)
        self.engine.current_time = 37
        with patch("abs_kids_player.playback.save_last_playback") as save:
            end = MenuCommand(CommandType.END_SELECTION, selection_key=first_key)
            handle_menu_command(end, self.menu, self.owner)
            save.assert_called_once_with("first", "first", 37, episode_id="", account="")
            self.assertEqual(self.menu.books[0].current_time, 37)
            self.assertIsNone(self.owner.state.completed_session)
            self.choose("first")
            self.wait_for_result()
            self.menu.set_session(self.owner.session, session_key=self.owner.state.session_key)
            self.menu.screen = Screen.LISTENING
            self.menu.listening_index = [key for key, _ in self.menu.listening_options].index("stop")
            knob_stop, = self.menu.click_nav()
            self.assertNotEqual(knob_stop.selection_key, first_key)
            handle_menu_command(end, self.menu, self.owner)
            self.assertTrue(self.owner.is_playing)
            self.owner.pause()
            handle_menu_command(knob_stop, self.menu, self.owner)
            self.assertIsNone(self.owner.session)
            self.assertEqual(save.call_count, 2)

    def test_reopening_paused_book_from_cover_refreshes_position_and_assigns_new_request_key(self) -> None:
        config = AppConfig(server_url="https://books.test", token="key", library_id="lib")
        prepared = prepare_book("first")
        prepared.prepared.session.progress_account = progress_account(config)
        self.prepare.side_effect = lambda _book: prepared
        self.choose("first")
        self.wait_for_result()
        old_key = self.owner.state.session_key
        self.owner.pause()
        command = WebPlayerCommand("play_book", id="b" * 32, book_id="first", created_at=time.time(),
                                   catalogue_key=catalogue_request_key(config))
        with (patch.object(oled_service, "load_config", return_value=config),
              patch.object(oled_service, "remove_started_book"), patch.object(oled_service, "save_command_result"),
              patch("abs_kids_player.playback.save_last_playback")):
            refreshed = prepare_book("first")
            refreshed.prepared.session.current_time = 91
            self.prepare.side_effect = lambda _book: refreshed
            self.assertEqual(handle_web_book_request(command, 0, self.menu, self.owner), oled_service.BOOK_LOADING_FRAME)
            result = self.wait_for_result()
            self.assertEqual(result.session.current_time, 91)
            self.assertEqual(self.engine.started_books, ["first", "first"])
            self.assertEqual(self.owner.state.session_key, command.id)
            self.menu.set_session(result.session, session_key=self.owner.state.session_key)
            handle_menu_command(MenuCommand(CommandType.END_SELECTION, selection_key=old_key), self.menu, self.owner)
            self.assertTrue(self.owner.is_playing)
            handle_menu_command(MenuCommand(CommandType.END_SELECTION, selection_key=command.id), self.menu, self.owner)
            self.assertIsNone(self.owner.session)

    def test_web_stop_of_knob_book_saves_once_and_rejects_a_replayed_stop(self):
        self.choose("first")
        self.wait_for_result()
        config = AppConfig()
        command = WebPlayerCommand("stop", session_key=self.owner.state.session_key,
                                   catalogue_key=catalogue_status_key(config))
        with (patch.object(oled_service, "load_config", return_value=config),
              patch.object(oled_service, "consume_web_player_commands", return_value=[command]),
              patch.object(oled_service, "save_command_result") as report,
              patch("abs_kids_player.playback.save_last_playback") as save):
            handle_web_player_commands(self.owner, self.menu, 50, False)
            self.assertIsNone(self.owner.session)
            report.assert_called_with(command, "applied")
            self.choose("second")
            self.wait_for_result()
            handle_web_player_commands(self.owner, self.menu, 50, False)
            self.assertEqual(self.owner.session.library_item_id, "second")
            report.assert_called_with(command, "superseded")
            save.assert_called_once()

    def test_home_cancels_a_pending_book(self) -> None:
        entered, release = self.hold_preparation("first", prepare_book("first"))
        self.choose("first")
        self.assertTrue(entered.wait(timeout=2))

        handle_menu_command(MenuCommand(CommandType.HOME), self.menu, self.owner)
        self.finish_preparation("first", release)

        self.assertIsNone(self.owner.advance_book_request())
        self.assertFalse(self.owner.loading_book)
        self.assertEqual(self.engine.started_books, [])

    def test_web_pause_cancels_a_pending_book(self) -> None:
        entered, release = self.hold_preparation("first", prepare_book("first"))
        self.choose("first")
        self.assertTrue(entered.wait(timeout=2))

        with patch(
            "abs_kids_player.oled_service.consume_web_player_commands",
            return_value=[WebPlayerCommand("pause")],
        ):
            handle_web_player_commands(self.owner, self.menu, 50, False)
        self.finish_preparation("first", release)

        self.assertIsNone(self.owner.advance_book_request())
        self.assertFalse(self.owner.loading_book)
        self.assertEqual(self.engine.started_books, [])

    def test_browser_cover_pause_never_cancels_a_newer_book_selection(self) -> None:
        config = AppConfig(server_url="https://books.example.test", token="token", library_id="lib")
        self.choose("first")
        self.wait_for_result()
        command = WebPlayerCommand("pause_book", catalogue_key=catalogue_request_key(config), book_id="first")
        entered, release = self.hold_preparation("second", prepare_book("second"))
        self.choose("second")
        self.assertTrue(entered.wait(timeout=2))
        with (
            patch.object(oled_service, "consume_web_player_commands", return_value=[command]),
            patch.object(oled_service, "load_config", return_value=config),
            patch.object(oled_service, "save_command_result") as report,
        ):
            handle_web_player_commands(self.owner, self.menu, 50, False)
        report.assert_called_once_with(command, "rejected")
        self.finish_preparation("second", release)
        self.assertEqual(self.wait_for_result().session.library_item_id, "second")
        self.assertTrue(self.owner.is_playing)

    def test_new_selection_can_cancel_preroll_before_the_old_book_activates(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def preroll(book_id: str) -> None:
            if book_id == "first":
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("Test did not release the playback thread")

        self.engine.before_start.side_effect = preroll
        self.choose("first")
        deadline = time.monotonic() + 2
        while not entered.is_set() and time.monotonic() < deadline:
            self.owner.advance_book_request()
            entered.wait(timeout=0.001)
        self.assertTrue(entered.is_set())

        self.choose("second")
        release.set()

        self.assertEqual(self.wait_for_result().session.library_item_id, "second")
        self.assertEqual(self.engine.started_books, ["second"])

    def test_failed_pipeline_start_reports_an_error_and_clears_loading(self) -> None:
        self.engine.before_start.side_effect = PlaybackError("could not start")
        self.choose("first")

        result = self.wait_for_result()
        self.assertIsNone(result.session)
        self.assertEqual(result.error_frame, BOOK_PLAYBACK_ERROR_FRAME)
        self.assertFalse(self.owner.loading_book)
        self.assertIsNone(self.owner.session)

    def test_failed_teardown_reports_one_book_error_and_allows_another_selection(self):
        with (patch.object(self.engine, "start", side_effect=PlaybackError("failed to start")),
              patch.object(self.engine, "release_pipeline", side_effect=OSError("failed to stop"))):
            self.choose("first")
            self.assertEqual(self.wait_for_result().error_frame, BOOK_PLAYBACK_ERROR_FRAME)
            self.assertFalse(self.owner.loading_book)
            self.assertIsNone(self.owner.advance_book_request())
        self.choose("next")
        self.assertEqual(self.wait_for_result().session.library_item_id, "next")

    def test_progress_write_failure_after_start_keeps_the_accepted_selection(self):
        self.engine.progress_sync = Mock()
        with patch.object(self.engine, "request_progress_sync", side_effect=OSError("disk full")) as save:
            self.owner.request_book(MenuCommand(CommandType.PLAY_BOOK, book_id="book", start_over=True))
            self.assertEqual(self.wait_for_result().session.library_item_id, "book")
            self.assertFalse(self.owner.loading_book)
            self.assertTrue(self.owner.is_playing)
            save.assert_called_once_with()

    def test_shutdown_discards_a_late_book_without_waiting_for_audiobookshelf(self):
        entered, release = self.hold_preparation("book", prepare_book("book"))
        self.choose("book")
        self.assertTrue(entered.wait(2))
        self.owner.close()
        self.assertTrue(self.preparation_threads["book"].is_alive())
        self.finish_preparation("book", release)
        self.assertIsNone(self.owner.advance_book_request())
        self.assertFalse(self.owner.loading_book)
        self.assertEqual(self.engine.started_books, [])

    def test_cancelling_a_queued_command_keeps_the_worker_available_for_the_next_one(self):
        entered, release = threading.Event(), threading.Event()
        applied = []

        def set_volume(percent, muted):
            applied.append(percent)
            if percent == 10:
                entered.set()
                self.assertTrue(release.wait(2))

        with patch.object(self.engine, "set_volume_state", side_effect=set_volume):
            running = self.owner.set_volume_state(10, False)
            try:
                self.assertTrue(entered.wait(2))
                abandoned = self.owner.set_volume_state(11, False)
                following = self.owner.set_volume_state(12, False)
                self.assertTrue(abandoned.cancel())
            finally:
                release.set()
            running.result(timeout=2)
            following.result(timeout=2)
        self.assertEqual(applied, [10, 12])

    def test_shutdown_rejects_new_work_while_the_engine_is_stopping(self):
        stopping, release = threading.Event(), threading.Event()

        def stop():
            stopping.set()
            self.assertTrue(release.wait(2))

        with patch.object(self.engine, "stop", side_effect=stop) as stop_engine:
            closer = threading.Thread(target=self.owner.close)
            closer.start()
            try:
                self.assertTrue(stopping.wait(2))
                with self.assertRaisesRegex(RuntimeError, "closed"):
                    self.owner.set_volume_state(20, False)
                with self.assertRaisesRegex(RuntimeError, "closed"):
                    self.owner.request_book(MenuCommand(CommandType.PLAY_BOOK, book_id="late"))
            finally:
                release.set()
                closer.join(timeout=2)
            self.assertFalse(closer.is_alive())
            self.owner.close()
            stop_engine.assert_called_once_with()
            self.prepare.assert_not_called()

    def test_successful_preparation_and_playback_use_separate_threads(self) -> None:
        self.choose("book")
        self.wait_for_result()
        self.owner.set_volume_state(30, False)
        self.owner.pause_for_mute()
        self.owner.resume_after_mute(25, False)
        handle_menu_command(MenuCommand(CommandType.PAUSE_PLAYBACK), self.menu, self.owner)
        with patch(
            "abs_kids_player.oled_service.consume_web_player_commands",
            return_value=[WebPlayerCommand("play")],
        ):
            handle_web_player_commands(self.owner, self.menu, 25, False)
        self.owner.refresh_audio_output()
        self.owner.stop()

        operations, threads = zip(*self.engine.operations)
        self.assertEqual(
            operations,
            (
                "start",
                "stop",
                "volume",
                "pause",
                "resume",
                "volume",
                "pause",
                "resume",
                "volume",
                "output",
                "stop",
            ),
        )
        self.assertEqual(len(set(threads)), 1)
        self.assertNotEqual(threads[0], threading.current_thread())
        self.assertNotEqual(threads[0], self.preparation_threads["book"])
