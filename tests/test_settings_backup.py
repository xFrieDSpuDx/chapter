from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from dataclasses import replace
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from check_download_reboot import ROOT, run_phase

from abs_kids_player.config import AppConfig, PodcastConfig, load_config, save_config
from abs_kids_player.library import ShelfOption
from abs_kids_player.owner_auth import (
    COOKIE_NAME,
    OwnerSessions,
    change_public_access,
    load_credential,
    set_password,
)
from abs_kids_player.playback_start import prepare_book_playback
from abs_kids_player.progress_sync import ProgressSync, progress_account, read_pending_progress
from abs_kids_player.settings_backup import (
    BackupError,
    apply_preview,
    backup_payload,
    preview_ticket,
    read_backup,
)
from abs_kids_player.setup_server import SetupHandler


def backup(**settings) -> str:
    return json.dumps({"version": 1, "settings": settings})


def post(path: str, *, cookie: str = "", **fields) -> SetupHandler:
    body = urlencode({"csrf_token": "csrf", **fields}).encode()
    handler = object.__new__(SetupHandler)
    handler.path = path
    handler.server = SimpleNamespace(csrf_token="csrf")
    handler.headers = {"Content-Length": str(len(body)), "Cookie": cookie}
    handler.rfile = io.BytesIO(body)
    handler.send_html = Mock()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.do_POST()
    return handler


def input_value(page: str, name: str) -> str:
    return unescape(re.search(fr'name="{name}" value="([^"]*)"', page).group(1))


class SettingsBackupTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {
            "ABS_KIDS_PLAYER_CONFIG_PATH": str(self.root / ".config/abs-kids-player/config.json"),
            "ABS_KIDS_PLAYER_STATE_PATH": str(self.root / ".local/state/abs-kids-player/player-state.json"),
            "ABS_KIDS_PLAYER_STATE_DIR": str(self.root / ".local/state/abs-kids-player"),
            "CHAPTER_OWNER_FILE": str(self.root / "owner.json"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_export_includes_only_the_supported_portable_preferences(self):
        config = AppConfig(
            server_url="http://private", token="token-secret", refresh_token="refresh-secret",
            username="private-account", library_id="private-library", screen_saver_dim_percent=5,
            spoken_navigation_volume_percent=15, pinned_shelf="collections/family",
            podcasts=[PodcastConfig("https://example.test/?token=podcast-secret")],
        )
        handler = object.__new__(SetupHandler)
        handler.path = "/system/settings-backup"
        handler.send_json = Mock()
        save_config(config)
        handler.do_GET()

        payload = handler.send_json.call_args.args[0]
        self.assertEqual(handler.send_json.call_args.kwargs, {"attachment": "chapter-settings.json"})
        self.assertEqual(read_backup(json.dumps(payload)), backup_payload(config)["settings"])
        for secret in ("token", "private", "podcast"):
            self.assertNotIn(secret, json.dumps(payload))
        self.assertEqual(payload["settings"]["screen_saver_dim_percent"], 5)
        self.assertEqual(payload["settings"]["pinned_shelf"], "collections/family")

    def test_invalid_versions_values_or_fields_are_rejected(self):
        for raw in ("{", "[]", '{"version":2,"settings":{}}', backup(token="abc"),
                    backup(night_start="99:00"), backup(night_mode="sometimes"),
                    backup(control_click_enabled=1), backup(night_volume_limit=101),
                    backup(pinned_shelf="collections/a/b"), '{"version":' + "1" * 4400 + "}"):
            with self.subTest(raw=raw[:80]), self.assertRaises(BackupError):
                read_backup(raw)

    def test_restore_requires_an_accurate_preview_preserves_login_and_updates_live_catalogue(self):
        current = AppConfig(server_url="http://books", token="local-token", username="local-user",
                            library_id="library", screen_saver_dim_percent=10)
        save_config(current)
        raw = backup(screen_saver_dim_percent=5, library_sort_mode="author_first", pinned_shelf="collections/shelf")
        with patch("abs_kids_player.setup_server.available_shelves",
                   return_value=[ShelfOption("collections/shelf", "Family & friends")]):
            preview = post("/system/settings-preview", backup=raw)
        self.assertEqual(load_config(), current)
        status, page = preview.send_html.call_args.args
        self.assertEqual(status, 200)
        self.assertIn("Family &amp; friends", page)
        self.assertIn("Author first name", page)
        payload, ticket = input_value(page, "backup"), input_value(page, "preview")

        rejected = post("/system/settings-restore", backup=raw)
        self.assertEqual(rejected.send_html.call_args.args[0], 400)
        with patch("abs_kids_player.setup_server.queue_web_player_command") as queue:
            restored = post("/system/settings-restore", backup=payload, preview=ticket)
        self.assertEqual(restored.send_html.call_args.args[0], 200)
        self.assertEqual(load_config(), replace(current, screen_saver_dim_percent=5, library_sort_mode="author_first",
                                               pinned_shelf="collections/shelf"))
        self.assertEqual([call.args[0] for call in queue.call_args_list], ["resort_library", "reload_catalogue"])

    def test_owner_restore_preserves_offline_books_queue_and_listening_position(self):
        (self.root / ".chapter-test-home").touch()
        run_phase(self.root, ROOT, "seed")
        current = load_config()
        account = progress_account(current)
        ProgressSync().record(account, "first", 17, 60)

        sessions = OwnerSessions()
        self.enterContext(patch("abs_kids_player.setup_server.owner_sessions", sessions))
        set_password("a sample owner password", "reader")
        change_public_access(load_credential(), overview=False, library=True)
        cookie = f"{COOKIE_NAME}={sessions.sign_in('reader', 'a sample owner password')}"
        raw = backup(download_limit_mib=1, screen_saver_dim_percent=5)
        preview = post("/system/settings-preview", cookie=cookie, backup=raw)
        page = preview.send_html.call_args.args[1]
        fields = {"backup": input_value(page, "backup"), "preview": input_value(page, "preview")}

        visitor = post("/system/settings-restore", **fields)
        visitor.send_header.assert_any_call("Location", "/owner")
        self.assertEqual(load_config(), current)
        owner = post("/system/settings-restore", cookie=cookie, **fields)
        self.assertEqual(owner.send_html.call_args.args[0], 200)
        self.assertEqual(load_config(), replace(current, download_limit_mib=1, screen_saver_dim_percent=5))
        self.assertTrue(sessions.allows(cookie))
        self.assertEqual(sessions.access("").public_sections, ("library", "downloads"))

        run_phase(self.root, ROOT, "offline")
        run_phase(self.root, ROOT, "resume")
        self.assertEqual(read_pending_progress()[(account, "first", "")].current_time, 17)
        with patch("abs_kids_player.playback_start.AudiobookshelfClient.get_progress", side_effect=TimeoutError):
            self.assertEqual(prepare_book_playback("first", load_config(), local_only=True).session.current_time, 17)

    def test_preview_expires_if_settings_or_account_change_and_rejects_edited_content(self):
        current = AppConfig(server_url="http://books", token="token", username="someone", library_id="a")
        raw = backup(night_mode="on")
        ticket = preview_ticket(current, raw)
        save_config(current)
        with self.assertRaises(BackupError):
            apply_preview(backup(night_mode="off"), ticket)
        changed = replace(current, library_id="b")
        save_config(changed)
        with self.assertRaises(BackupError):
            apply_preview(raw, ticket)
        self.assertEqual(load_config(), changed)

    def test_unavailable_pinned_shelf_is_skipped_while_other_settings_can_be_restored(self):
        save_config(AppConfig())
        preview = post("/system/settings-preview", backup=backup(night_mode="on", pinned_shelf="collections/shelf"))
        _, page = preview.send_html.call_args.args
        self.assertIn("Pinned shelf skipped", page)
        raw = input_value(page, "backup")
        self.assertEqual(read_backup(raw), {"night_mode": "on"})
        post("/system/settings-restore", backup=raw, preview=input_value(page, "preview"))
        self.assertEqual(load_config(), AppConfig(night_mode="on"))

    def test_listening_page_saves_all_groups_together_and_keeps_other_preferences(self):
        current = AppConfig(token="local-token", control_click_enabled=False, night_mode="schedule")
        save_config(current)
        handler = post(
            "/settings/listening", listening_page="1", night_mode="on",
            night_start="22:00", night_end="06:30", night_dim_percent="5", night_volume_limit="9",
            sleep_timer_enabled="1", sleep_timer_minutes="45", sleep_rewind="1",
            skip_back_seconds="20", skip_forward_seconds="40",
        )
        handler.send_response.assert_called_once_with(303)
        handler.send_header.assert_any_call("Location", "/settings/listening?saved=1")
        self.assertEqual(load_config(), replace(
            current, night_mode="on", night_start="22:00", night_end="06:30", night_dim_percent=5,
            night_volume_limit=9, sleep_timer_enabled=True, sleep_timer_minutes=45, sleep_rewind=True,
            skip_back_seconds=20, skip_forward_seconds=40,
        ))

    def test_display_protection_settings_save_clear_and_restore(self):
        fields = dict(screen_saver_mode="clock", screen_saver_dim_percent="5", screen_saver_clock_format="24-hour")
        settings = "/settings/display"
        saved = post(settings, **fields, screen_off_minutes="15", screen_off_start="23:00", screen_off_end="07:00")
        saved.send_header.assert_any_call("Location", "/settings/display?saved=1")
        config = load_config()
        self.assertEqual((config.screen_off_minutes, config.screen_off_start, config.screen_off_end), (15, "23:00", "07:00"))
        raw = json.dumps(backup_payload(config))
        for changes in (
            dict(screen_off_start="22:00", screen_off_end=""), dict(screen_off_start="22:00", screen_off_end="22:00"),
            dict(screen_off_minutes="-1"),
        ):
            with self.subTest(changes=changes):
                rejected = post(settings, **fields, **changes)
                self.assertEqual(rejected.send_html.call_args.args[0], 400)
                self.assertEqual(load_config(), config)
        post(settings, **fields)  # A form opened before the upgrade.
        self.assertEqual(load_config(), config)
        post(settings, **fields, screen_off_minutes="0", screen_off_start="", screen_off_end="")
        self.assertEqual((load_config().screen_off_minutes, load_config().screen_off_start, load_config().screen_off_end),
                         (0, "", ""))
        apply_preview(raw, preview_ticket(load_config(), raw))
        self.assertEqual(load_config(), config)

    def test_book_and_sound_settings_save_only_their_group(self):
        current = AppConfig(token="local-token", night_mode="on")
        save_config(current)
        with patch("abs_kids_player.setup_server.queue_web_player_command") as queue:
            books = post("/settings/books", library_sort_mode="author_first", playback_speed="1.5")
            queue.assert_called_once_with("resort_library")
        books.send_header.assert_any_call("Location", "/settings/books?saved=1")
        sound = post("/settings/sound", spoken_navigation_enabled="1",
                     control_click_volume_percent="12", spoken_navigation_volume_percent="18")
        sound.send_header.assert_any_call("Location", "/settings/sound?saved=1")
        self.assertEqual(load_config(), replace(
            current, library_sort_mode="author_first", playback_speed=1.5, control_click_enabled=False,
            spoken_navigation_enabled=True, control_click_volume_percent=12, spoken_navigation_volume_percent=18,
        ))

    def test_manual_night_mode_updates_only_its_setting(self):
        current = AppConfig(night_start="23:00", night_end="06:00", night_volume_limit=11)
        save_config(current)
        for mode in ("on", "off", "schedule"):
            handler = post("/settings/night-mode", mode=mode)
            handler.send_response.assert_called_once_with(303)
            handler.send_header.assert_any_call("Location", "/?tab=settings#night-mode")
            self.assertEqual(load_config(), replace(current, night_mode=mode))
