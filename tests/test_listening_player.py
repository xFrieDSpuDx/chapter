import io
import time
import unittest
from concurrent.futures import Future
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from abs_kids_player.bedtime import BedtimeTimer
from abs_kids_player.catalogue_settings import catalogue_status_key
from abs_kids_player.config import AppConfig
from abs_kids_player.listening_control import apply_listening_control
from abs_kids_player.listening_queue import edit_queue, read_queue, remove_started_book
from abs_kids_player.listening_status import (
    ListeningDetailsPublisher,
    listening_details,
    listening_progress,
)
from abs_kids_player.models import Book, Chapter, PlaybackSession, SeriesMembership
from abs_kids_player.oled_service import (
    finish_naturally,
    handle_web_player_commands,
    web_status_for_playback_state,
)
from abs_kids_player.playback_owner import PlaybackSnapshot
from abs_kids_player.player_control import (
    command_result,
    load_web_player_status,
    queue_web_player_command,
    save_web_player_status,
)
from abs_kids_player.progress_sync import PendingProgress, ProgressSyncStatus, progress_account
from abs_kids_player.rotary_ui import ApplianceMenu
from abs_kids_player.setup_server import SetupHandler


class ListeningPlayerTest(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict("os.environ", {
            "ABS_KIDS_PLAYER_STATE_DIR": str(directory),
            "CHAPTER_OWNER_FILE": str(directory / "owner.json"),
        }))
        self.config = AppConfig(server_url="https://books.example.test", token="token", username="reader", library_id="lib")
        self.account = progress_account(self.config)
        self.catalogue = catalogue_status_key(self.config)
        self.session = PlaybackSession(
            "abs-session", "first", "First", "Author", 600, 50, "", [],
            [Chapter(1, "The start", 0, 50), Chapter(2, "Middle", 50, 600)],
            progress_account=self.account, downloaded=True, library_id="lib",
        )
        self.state = PlaybackSnapshot(session=self.session, session_key="live", current_time=50, playback_rate=1.5)
        self.status = web_status_for_playback_state(self.state, 30, False)
        self.enterContext(patch("abs_kids_player.setup_server.load_config", return_value=self.config))
        self.enterContext(patch("abs_kids_player.listening_control.load_config", return_value=self.config))

    def handler(self):
        handler = object.__new__(SetupHandler)
        handler.send_json = Mock()
        handler.send_html = Mock()
        handler.send_owner_required = Mock()
        handler.send_csrf_error = Mock()
        handler.send_player_command_response = Mock()
        handler.csrf_token = Mock(return_value="csrf")
        handler.headers = {"X-Requested-With": "fetch"}
        return handler

    def post(self, handler, path, **fields):
        body = urlencode(fields).encode()
        handler.path = path
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler.do_POST()

    def test_player_tab_is_available_for_paused_book_and_hidden_when_idle(self):
        handler = self.handler()
        handler.path = "/player"
        save_web_player_status(self.status)
        handler.do_GET()
        page = handler.send_html.call_args.args[1]
        self.assertIn('href="/player" aria-current="page" data-player-navigation>Player</a>', page)
        self.assertIn('data-player-toggle aria-label="Play"', page)
        self.assertIn('data-player-session>', page)
        save_web_player_status(type(self.status)())
        handler.do_GET()
        page = handler.send_html.call_args.args[1]
        self.assertIn('data-player-navigation hidden', page)
        self.assertIn('data-player-session hidden', page)
        self.assertIn('id="listening-queue"', page)

    def test_player_actions_check_csrf_and_session_and_validate_values(self):
        save_web_player_status(self.status)
        handler = self.handler()
        common = {"session_key": "live", "action": "seek", "value": "50000"}
        self.post(handler, "/player/control", **common)
        handler.send_csrf_error.assert_called_once()
        self.post(handler, "/player/control", csrf_token="csrf", **{**common, "session_key": "old"})
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 409)
        self.post(handler, "/player/control", csrf_token="csrf", **{**common, "value": "900000"})
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)
        handler.send_player_command_response.assert_not_called()
        self.post(handler, "/player/control", csrf_token="csrf", **common)
        handler.send_player_command_response.assert_called_once()

    def test_quarter_step_speed_commands_reach_the_active_session(self):
        save_web_player_status(self.status)
        handler = self.handler()
        playback = Mock(state=self.state, loading_book=False)
        playback.set_session_playback_rate.return_value = True
        menu = ApplianceMenu()
        for value, rate in (("125", 1.25), ("175", 1.75)):
            self.post(handler, "/player/control", csrf_token="csrf", session_key="live", action="speed", value=value)
            receipt = handler.send_player_command_response.call_args.args[0]
            handle_web_player_commands(
                playback, menu, 30, False,
                listening_control=lambda cmd: apply_listening_control(cmd, playback, menu, self.config, Mock()),
            )
            playback.set_session_playback_rate.assert_called_with("live", rate)
            self.assertEqual(command_result(receipt), "applied")

    def test_opening_controls_are_available_before_the_first_session_and_scoped(self):
        handler = self.handler()
        opening = "a" * 32
        save_web_player_status(replace(type(self.status)(), opening_key=opening))
        handler.path = "/player"
        handler.do_GET()
        markup = handler.send_html.call_args.args[1]
        self.assertIn('data-player-navigation>Player</a>', markup)
        self.assertIn(f'data-player-opening="{opening}">', markup)
        handler.path = "/player/state"
        handler.do_GET()
        self.assertEqual(handler.send_json.call_args.args[0]["openingKey"], opening)
        self.post(handler, "/player/control", action="cancel_open", session_key=opening)
        handler.send_csrf_error.assert_called_once()
        self.post(handler, "/player/control", csrf_token="csrf", action="cancel_open", session_key="")
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 409)
        self.post(handler, "/player/control", csrf_token="csrf", action="cancel_open", session_key=opening)
        handler.send_player_command_response.assert_called_once()
        self.post(handler, "/player/control", csrf_token="csrf", action="stop", session_key=opening)
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 409)
        save_web_player_status(self.status)
        self.post(handler, "/player/control", csrf_token="csrf", action="stop", session_key="live")
        self.assertEqual(handler.send_player_command_response.call_count, 2)

    def test_player_renders_timer_choices_and_named_listening_tabs(self):
        class Controls(HTMLParser):
            def handle_starttag(self, tag, attrs):
                elements.append((tag, dict(attrs)))

        elements = []
        handler = self.handler()
        handler.path = "/player"
        save_web_player_status(self.status)
        handler.do_GET()
        Controls().feed(handler.send_html.call_args.args[1])
        buttons = [attrs for tag, attrs in elements if tag == "button"]
        timer = next(i for i, (tag, attrs) in enumerate(elements) if "data-player-timer" in attrs)
        self.assertEqual([attrs["value"] for tag, attrs in elements[timer + 1:timer + 5]], ["0", "15", "30", "-1"])
        tools = [button for button in buttons if button.get("role") == "tab"]
        panels = {attrs.get("id") for tag, attrs in elements if tag == "section" and "hidden" in attrs}
        self.assertEqual([button["aria-controls"] for button in tools], ["player-bookmarks", "listening-queue", "player-session-actions"])
        for i, button in enumerate(tools):
            self.assertEqual(button["aria-controls"] in panels, i != 0)
            self.assertEqual(button["aria-selected"], "true" if i == 0 else "false")
        self.assertIn(("option", {"value": "150", "selected": None}), elements)

    def test_sleep_preset_reflects_the_device_after_reload_cancel_and_expiry(self):
        timer = BedtimeTimer()
        details = ListeningDetailsPublisher()
        now = time.monotonic()

        def preset():
            details.publish(self.state, None, timer)
            return listening_details(self.status)["sleepPreset"]

        self.assertEqual(preset(), 0)
        for minutes in (15, 30, -1):
            timer.arm(self.session.id, now, minutes, 600, rewind=False)
            self.assertEqual(preset(), minutes)
        timer.cancel()
        self.assertEqual(preset(), 0)
        timer.arm(self.session.id, now, 15, 600, rewind=False)
        self.assertEqual(preset(), 15)
        timer.update(session_id=self.session.id, now=now + 900, position=50, speed=1, playing=True)
        self.assertEqual(preset(), 0)
        self.assertTrue(listening_details(self.status)["sleeping"])

    def test_owner_rejects_control_from_an_older_session_of_the_same_book(self):
        old = queue_web_player_command("pause", session_key="old", catalogue_key=self.catalogue)
        correct = queue_web_player_command("skip_forward", session_key="live", catalogue_key=self.catalogue)
        playback = Mock()
        playback.loading_book = False
        playback.state = self.state
        callback = Mock(return_value=True)
        handle_web_player_commands(playback, ApplianceMenu(), 30, False, listening_control=callback)
        playback.pause.assert_not_called()
        callback.assert_called_once_with(correct)
        self.assertEqual(command_result(old.id), "superseded")
        self.assertEqual(command_result(correct.id), "applied")

    def test_skip_and_undo_share_the_menu_position_and_preserve_pause(self):
        playback = Mock()
        playback.current_time = 50
        playback.is_playing = False
        menu = ApplianceMenu()
        menu.set_session(self.session, is_playing=False)

        def seek(position):
            playback.current_time = position
            return True

        playback.seek_global.side_effect = seek
        for action, position in (("skip_forward", 80), ("undo", 50)):
            command = queue_web_player_command(action)
            self.assertTrue(apply_listening_control(command, playback, menu, self.config, Mock()))
            self.assertEqual(menu.current_time, position)
            self.assertFalse(menu.is_playing)
        self.assertIsNone(menu.undo_position)

    def test_bookmark_acknowledgement_waits_for_audiobookshelf(self):
        command = queue_web_player_command("bookmark", session_key="live", catalogue_key=self.catalogue)
        playback = Mock()
        playback.state = self.state
        playback.loading_book = False
        future: Future[bool] = Future()
        playback.save_bookmark.return_value = future
        menu = ApplianceMenu()
        def apply(cmd):
            return apply_listening_control(cmd, playback, menu, self.config, Mock())

        handle_web_player_commands(playback, menu, 30, False, listening_control=apply)
        self.assertEqual(command_result(command.id), "queued")
        future.set_result(False)
        self.assertEqual(command_result(command.id), "rejected")

    def test_failed_speed_change_has_a_receipt_and_does_not_block_volume(self):
        speed = queue_web_player_command("speed", 200, session_key="live", catalogue_key=self.catalogue)
        volume = queue_web_player_command("volume", 35, session_key="live", catalogue_key=self.catalogue)
        playback = Mock(state=self.state, loading_book=False)
        pending = Future()
        playback.set_session_playback_rate.return_value = pending
        playback.set_volume_state.return_value = None
        menu = ApplianceMenu()
        handle_web_player_commands(
            playback, menu, 30, False,
            listening_control=lambda cmd: apply_listening_control(cmd, playback, menu, self.config, Mock()),
        )
        playback.set_session_playback_rate.assert_called_once_with("live", 2.0)
        self.assertEqual(command_result(volume.id), "applied")
        self.assertEqual(command_result(speed.id), "queued")
        pending.set_exception(OverflowError("seek"))
        self.assertEqual(command_result(speed.id), "rejected")

    def test_web_state_exposes_downloaded_source_and_only_active_session_details(self):
        timer = BedtimeTimer()
        timer.arm(self.session.id, time.monotonic(), -1, 600, False)
        ListeningDetailsPublisher().publish(self.state, 25, timer)
        self.assertEqual(listening_details(replace(self.status, session_key="different")), {})
        save_web_player_status(self.status)
        handler = self.handler()
        handler.path = "/player/state"
        handler.do_GET()
        result = handler.send_json.call_args.args[0]
        self.assertEqual(result["source"], "Downloaded copy")
        self.assertEqual(result["speed"], 150)
        self.assertEqual(result["sleepChapterEnd"], 600)
        self.assertEqual(result["chapters"][1]["start"], 50)
        self.assertTrue(result["canUndo"])
        self.assertNotIn("account", result)

    def test_offline_saved_position_and_later_acknowledgement_are_item_scoped(self):
        pending = PendingProgress(self.account, "first", 73, 600, 2000)
        with (
            patch("abs_kids_player.listening_status.read_listening_progress", return_value={pending.key: pending}) as positions,
            patch("abs_kids_player.listening_status.load_progress_sync_status", return_value=ProgressSyncStatus(
                outcome="retrying", item_id="different",
            )) as attempt,
        ):
            self.assertEqual(listening_progress(self.config, self.status)["state"], "waiting")
            attempt.return_value = replace(attempt.return_value, item_id="first")
            self.assertEqual(listening_progress(self.config, self.status)["state"], "retrying")
            positions.return_value = {pending.key: replace(pending, synced=True)}
            self.assertEqual(listening_progress(self.config, self.status)["state"], "synced")
            self.assertEqual(listening_progress(self.config, replace(self.status, cover_item_id="different"))["state"], "not_saved")
            self.assertEqual(listening_progress(replace(self.config, username="other"), self.status), {})

    def test_queue_persists_reordering_and_isolated_library_settings(self):
        handler = self.handler()
        with patch("abs_kids_player.setup_server.books_with_downloads", return_value=[
            Book("first", "First", "", 60, ""), Book("second", "Second", "", 60, ""),
        ]):
            for action, item in (("add", "first"), ("add", "second"), ("add", "second"), ("up", "second")):
                self.post(handler, "/player/queue", csrf_token="csrf", action=action, book_id=item)
                handler.send_json.assert_called_with({"saved": True})
        self.post(handler, "/player/queue", csrf_token="csrf", action="auto", enabled="1")
        self.assertEqual(read_queue(self.catalogue).items, ("second", "first"))
        self.assertTrue(read_queue(self.catalogue).auto_advance)
        self.assertEqual(read_queue(catalogue_status_key(replace(self.config, library_id="other"))).items, ())
        remove_started_book(self.config, self.session)
        self.assertEqual(read_queue(self.catalogue).items, ("second",))
        self.post(handler, "/player/queue", csrf_token="csrf", action="remove", book_id="second")
        self.assertEqual(read_queue(self.catalogue).items, ())

    def test_auto_advance_prefers_queue_then_series_and_checks_scope(self):
        def series(number):
            return (SeriesMembership("series", "Series", number),)

        first = Book("first", "First", "", 60, "", series_memberships=series("1"))
        second = Book("second", "Second", "", 60, "", series_memberships=series("2"))
        queued = Book("queued", "Queued", "", 60, "")
        menu = ApplianceMenu([first, second, queued])
        self.assertIsNone(finish_naturally(menu, self.config, self.session, can_advance=True))
        edit_queue(self.catalogue, "auto", "1")
        edit_queue(self.catalogue, "add", "queued")
        self.assertEqual(finish_naturally(menu, self.config, self.session, can_advance=True), queued)
        self.assertEqual(read_queue(self.catalogue).items, ("queued",))
        self.assertIsNone(finish_naturally(menu, self.config, self.session, can_advance=False))
        edit_queue(self.catalogue, "remove", "queued")
        self.assertEqual(finish_naturally(menu, self.config, self.session, can_advance=True), second)
        first.is_finished = False
        self.assertIsNone(finish_naturally(menu, replace(self.config, library_id="other"), self.session, can_advance=True))
        self.assertFalse(first.finished)
        self.assertIsNone(finish_naturally(menu, self.config, replace(self.session, episode_id="episode"), can_advance=True))
        self.assertEqual(load_web_player_status().session_key, "")
