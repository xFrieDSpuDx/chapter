from __future__ import annotations

import io
import os
import re
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from abs_kids_player import __version__
from abs_kids_player.bluetooth_audio import BluetoothDevice
from abs_kids_player.config import (
    CLOCK_FORMAT_24_HOUR,
    LIBRARY_SORT_AUTHOR,
    LIBRARY_SORT_TITLE,
    SCREEN_SAVER_BOOKS,
    AppConfig,
    PodcastConfig,
)
from abs_kids_player.config import (
    load_config as read_settings,
)
from abs_kids_player.config import (
    save_config as write_settings,
)
from abs_kids_player.connection_health import ConnectionHealth, ConnectionMonitor, ConnectionState
from abs_kids_player.player_control import WebPlayerStatus
from abs_kids_player.setup_http import MAX_POST_BODY_BYTES
from abs_kids_player.setup_server import (
    SYSTEM_ACTION_DELAY_SECONDS,
    WIFI_CHANGE_DELAY_SECONDS,
    SetupHandler,
    bluetooth_status,
    login_and_refresh_player,
    login_username,
    restore_device_defaults,
    save_click_setting,
    save_library_sort_setting,
    save_playback_speed_setting,
    save_podcast_settings,
    save_screen_saver_setting,
    save_sleep_timer_setting,
    save_spoken_navigation_setting,
    schedule_software_update,
    schedule_system_reboot,
    schedule_system_reset,
    schedule_wifi_forget,
)
from abs_kids_player.software_update import SoftwareUpdateState
from abs_kids_player.web_player_events import (
    player_status_event_signature,
    player_status_payload,
    player_status_sse,
)
from abs_kids_player.wifi import WifiStatus


class FakeAudiobookshelfClient:
    token = "token"
    refresh_token = "refresh-token"

    def __init__(self, _server_url: str, _token: str) -> None:
        pass

    @classmethod
    def login(cls, _server_url: str, _username: str, _password: str):
        return cls(_server_url, cls.token), {"user": {"username": "chapter"}}

    def choose_library_id(self, _library_id: str = "") -> str:
        return "library-1"

    def get_current_user(self) -> dict:
        return {"user": {"username": "chapter"}}


class SetupServerTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"ABS_KIDS_PLAYER_CONFIG_PATH": directory + "/config.json"}))

    def test_player_fetch_returns_a_receipt_that_can_be_queried(self) -> None:
        body = b"action=pause"
        handler = object.__new__(SetupHandler)
        handler.headers = {"Content-Length": str(len(body)), "X-Requested-With": "fetch"}
        handler.rfile = io.BytesIO(body)
        handler.send_json = Mock()
        with patch("abs_kids_player.setup_server.queue_web_player_command",
                   return_value=SimpleNamespace(id="command")):
            handler.handle_player_post()
        handler.send_json.assert_called_once_with({"id": "command", "outcome": "queued"}, status=202)

        handler.path = "/player/command?id=command"
        with (
            patch("abs_kids_player.setup_server.command_result", return_value="applied") as status,
            patch("abs_kids_player.setup_server.time.time", return_value=120.5),
        ):
            handler.do_GET()
        status.assert_called_once_with("command")
        handler.send_json.assert_called_with({"outcome": "applied", "observedAt": 120.5})

    def test_saved_login_does_not_add_redundant_page_message(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        config = AppConfig(server_url="https://books.example.com", token="token", username="chapter")

        with (
            patch("abs_kids_player.setup_server.load_config", return_value=config),
            patch(
                "abs_kids_player.setup_server.wifi_status",
                return_value=WifiStatus(connected=True, ssid="Chapter WiFi"),
            ),
            patch("abs_kids_player.setup_server.connection_monitor") as monitor,
            patch("abs_kids_player.setup_server.load_web_player_status", return_value=WebPlayerStatus()),
            patch("abs_kids_player.setup_server.bluetooth_status", return_value=(False, "Not connected")),
        ):
            monitor.status_for.return_value = ConnectionHealth(ConnectionState.CONNECTED, "chapter")
            handler.send_page()

        page = handler.wfile.getvalue().decode("utf-8")
        self.assertNotIn("This player already has an Audiobookshelf login saved.", page)
        self.assertIn("Signed in as chapter.", page)

    def test_admin_page_renders_while_audiobookshelf_verification_is_blocked(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_check(_config: AppConfig) -> ConnectionHealth:
            started.set()
            release.wait(timeout=2)
            return ConnectionHealth(ConnectionState.CONNECTED, "chapter")

        monitor = ConnectionMonitor(slow_check)
        config = AppConfig(server_url="https://books.example.com", token="token", username="chapter")
        handler = object.__new__(SetupHandler)
        handler.send_html = Mock()

        try:
            with (
                patch("abs_kids_player.setup_server.connection_monitor", monitor),
                patch("abs_kids_player.setup_server.load_config", return_value=config),
                patch("abs_kids_player.setup_server.wifi_status", return_value=WifiStatus(connected=False)),
                patch("abs_kids_player.setup_server.load_web_player_status", return_value=WebPlayerStatus()),
                patch("abs_kids_player.setup_server.bluetooth_status", return_value=(False, "Not connected")),
            ):
                handler.send_page()
                self.assertTrue(started.wait(timeout=1))
                page = handler.send_html.call_args.args[1]
                self.assertIn("Checking connection", page)
                self.assertNotIn("Library accessible", page)
        finally:
            release.set()

    def test_audiobookshelf_endpoint_returns_the_latest_check(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.path = "/status/audiobookshelf"
        handler.send_html = Mock()
        with (
            patch("abs_kids_player.setup_server.load_config", return_value=AppConfig()),
            patch("abs_kids_player.setup_server.connection_monitor") as monitor,
        ):
            monitor.status_for.return_value = ConnectionHealth(ConnectionState.SIGN_IN_REQUIRED)
            handler.do_GET()

        self.assertEqual(handler.send_html.call_args.args[0], 200)
        self.assertIn("Sign in again from Connections.", handler.send_html.call_args.args[1])

    def test_bluetooth_status_reports_connected_device_name(self) -> None:
        with patch(
            "abs_kids_player.setup_server.connected_bluetooth_audio_device",
            return_value=BluetoothDevice("AA:BB:CC:DD:EE:FF", "Playroom Speaker"),
        ):
            self.assertEqual(bluetooth_status(), (True, "Connected to Playroom Speaker"))

    def test_bluetooth_status_reports_not_connected(self) -> None:
        with patch("abs_kids_player.setup_server.connected_bluetooth_audio_device", return_value=None):
            self.assertEqual(bluetooth_status(), (False, "Not connected"))

    def test_player_status_payload_reports_contextual_button_state(self) -> None:
        payload = player_status_payload(
            WebPlayerStatus(title="The Hobbit", current_time=60, duration=300, is_playing=False, volume_percent=45)
        )

        self.assertTrue(payload["hasSession"])
        self.assertEqual(payload["title"], "The Hobbit")
        self.assertEqual(payload["state"], "Paused")
        self.assertEqual(payload["timeInfo"], "1:00 / 5:00")
        self.assertEqual(payload["currentTime"], 60)
        self.assertEqual(payload["duration"], 300)
        self.assertIn("updatedAt", payload)
        self.assertFalse(payload["isPlaying"])
        self.assertEqual(payload["volumePercent"], 45)

    def test_player_status_sse_formats_status_event(self) -> None:
        event = player_status_sse('{"isPlaying":true}')

        self.assertEqual(event, b'data: {"isPlaying":true}\n\n')

    def test_setup_handler_uses_http11_for_sse(self) -> None:
        from abs_kids_player.setup_server import SetupHandler

        self.assertEqual(SetupHandler.protocol_version, "HTTP/1.1")
        self.assertEqual(SetupHandler.server_version, f"ChapterPlayer/{__version__}")

    def test_player_status_event_signature_ignores_live_time_only(self) -> None:
        paused = player_status_payload(
            WebPlayerStatus(title="The Hobbit", current_time=60, duration=300, is_playing=False, volume_percent=45)
        )
        later = dict(paused)
        later["currentTime"] = 61
        later["updatedAt"] = paused["updatedAt"] + 1
        playing = dict(paused)
        playing["isPlaying"] = True
        playing["state"] = "Playing"

        self.assertEqual(player_status_event_signature(paused), player_status_event_signature(later))
        self.assertNotEqual(player_status_event_signature(paused), player_status_event_signature(playing))

    def test_save_click_setting_preserves_login_config(self) -> None:
        podcasts = [PodcastConfig(url="https://podcasts.apple.com/us/podcast/example/id123", title="Example")]
        config = AppConfig(
            server_url="https://books.example.com",
            token="token",
            library_id="library-1",
            username="chapter",
            control_click_enabled=True,
            screen_saver_mode=SCREEN_SAVER_BOOKS,
            podcasts=podcasts,
        )

        write_settings(config)
        updated = save_click_setting(False)

        self.assertEqual(updated.server_url, config.server_url)
        self.assertEqual(updated.token, config.token)
        self.assertEqual(updated.library_id, config.library_id)
        self.assertEqual(updated.username, config.username)
        self.assertFalse(updated.control_click_enabled)
        self.assertEqual(updated.screen_saver_mode, SCREEN_SAVER_BOOKS)
        self.assertEqual(updated.podcasts, podcasts)
        self.assertEqual(read_settings(), updated)

    def test_save_spoken_navigation_setting_preserves_other_config(self) -> None:
        config = AppConfig(
            server_url="https://books.example.com",
            token="token",
            username="chapter",
            control_click_enabled=False,
        )

        write_settings(config)
        updated = save_spoken_navigation_setting(True)

        self.assertTrue(updated.spoken_navigation_enabled)
        self.assertFalse(updated.control_click_enabled)
        self.assertEqual(updated.server_url, config.server_url)
        self.assertEqual(read_settings(), updated)

    def test_save_library_sort_setting_preserves_config_and_resorts_player(self) -> None:
        config = AppConfig(
            server_url="https://books.example.com",
            token="token",
            username="chapter",
            library_sort_mode=LIBRARY_SORT_TITLE,
        )

        write_settings(config)
        with (
            patch("abs_kids_player.setup_server.queue_web_player_command") as queue_command,
        ):
            updated = save_library_sort_setting(LIBRARY_SORT_AUTHOR)

        self.assertEqual(updated.library_sort_mode, LIBRARY_SORT_AUTHOR)
        self.assertEqual(updated.server_url, config.server_url)
        self.assertEqual(updated.token, config.token)
        self.assertEqual(read_settings(), updated)
        queue_command.assert_called_once_with("resort_library")

    def test_save_screen_saver_setting_preserves_login_config(self) -> None:
        podcasts = [PodcastConfig(url="https://podcasts.apple.com/us/podcast/example/id123", title="Example")]
        config = AppConfig(
            server_url="https://books.example.com",
            token="token",
            library_id="library-1",
            username="chapter",
            control_click_enabled=False,
            screen_saver_dim_percent=25,
            podcasts=podcasts,
        )

        write_settings(config)
        updated = save_screen_saver_setting(
            SCREEN_SAVER_BOOKS, 10, clock_format=CLOCK_FORMAT_24_HOUR
        )

        self.assertEqual(updated.server_url, config.server_url)
        self.assertEqual(updated.token, config.token)
        self.assertEqual(updated.library_id, config.library_id)
        self.assertEqual(updated.username, config.username)
        self.assertFalse(updated.control_click_enabled)
        self.assertEqual(updated.screen_saver_mode, SCREEN_SAVER_BOOKS)
        self.assertEqual(updated.screen_saver_dim_percent, 10)
        self.assertEqual(updated.screen_saver_clock_format, CLOCK_FORMAT_24_HOUR)
        self.assertEqual(updated.podcasts, podcasts)
        self.assertEqual(read_settings(), updated)

    def test_partial_screen_saver_settings_preserve_dim_level_and_clock_format(self) -> None:
        config = AppConfig(screen_saver_dim_percent=25, screen_saver_clock_format=CLOCK_FORMAT_24_HOUR)
        write_settings(config)
        updated = save_screen_saver_setting(SCREEN_SAVER_BOOKS)

        self.assertEqual(updated.screen_saver_mode, SCREEN_SAVER_BOOKS)
        self.assertEqual(updated.screen_saver_dim_percent, 25)
        self.assertEqual(updated.screen_saver_clock_format, CLOCK_FORMAT_24_HOUR)
        self.assertEqual(read_settings(), updated)

    def test_save_playback_speed_setting_preserves_other_config(self) -> None:
        config = AppConfig(
            server_url="https://books.example.com",
            token="token",
            username="chapter",
            playback_speed=1.0,
        )

        write_settings(config)
        updated = save_playback_speed_setting("1.5")

        self.assertEqual(updated.playback_speed, 1.5)
        self.assertEqual(updated.server_url, config.server_url)
        self.assertEqual(updated.token, config.token)
        self.assertEqual(read_settings(), updated)

    def test_save_sleep_timer_setting_preserves_other_config(self) -> None:
        config = AppConfig(
            server_url="https://books.example.com",
            token="token",
            username="chapter",
            screen_saver_mode=SCREEN_SAVER_BOOKS,
            sleep_timer_enabled=False,
            sleep_timer_minutes=30,
        )

        write_settings(config)
        enabled = save_sleep_timer_setting(enabled=True)
        updated = save_sleep_timer_setting(minutes=45)

        self.assertTrue(enabled.sleep_timer_enabled)
        self.assertEqual(enabled.sleep_timer_minutes, 30)
        self.assertTrue(updated.sleep_timer_enabled)
        self.assertEqual(updated.sleep_timer_minutes, 45)
        self.assertEqual(updated.server_url, config.server_url)
        self.assertEqual(updated.screen_saver_mode, SCREEN_SAVER_BOOKS)
        self.assertEqual(read_settings(), updated)

    def test_save_podcast_settings_resolves_urls_preserves_login_and_refreshes_library(self) -> None:
        config = AppConfig(
            server_url="https://books.example.com",
            token="token",
            library_id="library-1",
            username="chapter",
            podcasts=[],
        )
        resolved = PodcastConfig(
            url="https://podcasts.apple.com/us/podcast/example/id123",
            title="Example",
            author="Parent",
            feed_url="https://feeds.example.com/example.xml",
            book_id="podcast:apple:123",
        )

        write_settings(config)
        with (
            patch("abs_kids_player.setup_server.resolve_podcast_config", return_value=resolved) as resolve_podcast,
            patch("abs_kids_player.setup_server.queue_web_player_command") as queue_command,
        ):
            updated = save_podcast_settings(
                [
                    "",
                    "https://podcasts.apple.com/us/podcast/example/id123",
                    "https://podcasts.apple.com/us/podcast/example/id123",
                ]
            )

        resolve_podcast.assert_called_once_with("https://podcasts.apple.com/us/podcast/example/id123")
        self.assertEqual(updated.server_url, config.server_url)
        self.assertEqual(updated.username, config.username)
        self.assertEqual(updated.podcasts, [resolved])
        self.assertEqual(read_settings(), updated)
        queue_command.assert_called_once_with("refresh_library")

    def test_click_setting_post_redirects_home_after_save(self) -> None:
        body = b"enabled=0"
        handler = object.__new__(SetupHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.redirect_home = Mock()

        with patch("abs_kids_player.setup_server.save_click_setting") as save_setting:
            handler.handle_click_setting_post()

        save_setting.assert_called_once_with(False)
        handler.redirect_home.assert_called_once_with("settings")

    def test_spoken_navigation_setting_post_redirects_home_after_save(self) -> None:
        body = b"enabled=1"
        handler = object.__new__(SetupHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.redirect_home = Mock()

        with patch("abs_kids_player.setup_server.save_spoken_navigation_setting") as save_setting:
            handler.handle_spoken_navigation_setting_post()

        save_setting.assert_called_once_with(True)
        handler.redirect_home.assert_called_once_with("settings")

    def test_feedback_level_posts_preserve_the_other_setting_and_enabled_switches(self) -> None:
        initial = AppConfig(
            control_click_enabled=False, spoken_navigation_enabled=True,
            control_click_volume_percent=12, spoken_navigation_volume_percent=34,
        )
        for method, expected in (
            ("handle_click_setting_post", (7, 34)),
            ("handle_spoken_navigation_setting_post", (12, 7)),
        ):
            with self.subTest(method=method):
                body = b"volume_percent=7"
                handler = object.__new__(SetupHandler)
                handler.headers = {"Content-Length": str(len(body))}
                handler.rfile = io.BytesIO(body)
                handler.redirect_home = Mock()
                write_settings(initial)
                getattr(handler, method)()
                updated = read_settings()
                self.assertEqual(
                    (updated.control_click_volume_percent, updated.spoken_navigation_volume_percent),
                    expected,
                )
                self.assertFalse(updated.control_click_enabled)
                self.assertTrue(updated.spoken_navigation_enabled)
                handler.redirect_home.assert_called_once_with("settings")

    def test_library_sort_setting_post_redirects_home_after_save(self) -> None:
        body = b"library_sort_mode=author"
        handler = object.__new__(SetupHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.redirect_home = Mock()

        with patch("abs_kids_player.setup_server.save_library_sort_setting") as save_setting:
            handler.handle_library_sort_setting_post()

        save_setting.assert_called_once_with(LIBRARY_SORT_AUTHOR)
        handler.redirect_home.assert_called_once_with("settings", anchor="book-preferences")

    def test_screen_saver_setting_post_redirects_home_after_save(self) -> None:
        body = b"screen_saver_mode=books&screen_saver_dim_percent=10&screen_saver_clock_format=24-hour"
        handler = object.__new__(SetupHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.redirect_home = Mock()

        with patch("abs_kids_player.setup_server.save_screen_saver_setting") as save_setting:
            handler.handle_screen_saver_setting_post()

        save_setting.assert_called_once_with(SCREEN_SAVER_BOOKS, "10", clock_format=CLOCK_FORMAT_24_HOUR)
        handler.redirect_home.assert_called_once_with("settings", anchor="screen-saver")

    def test_playback_speed_setting_post_redirects_home_after_save(self) -> None:
        body = b"playback_speed=2"
        handler = object.__new__(SetupHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.redirect_home = Mock()

        with patch("abs_kids_player.setup_server.save_playback_speed_setting") as save_setting:
            handler.handle_playback_speed_setting_post()

        save_setting.assert_called_once_with("2")
        handler.redirect_home.assert_called_once_with("settings", anchor="book-preferences")

    def test_sleep_timer_toggle_post_redirects_home_after_save(self) -> None:
        body = b"enabled=1"
        handler = object.__new__(SetupHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.redirect_home = Mock()

        with patch("abs_kids_player.setup_server.save_sleep_timer_setting") as save_setting:
            handler.handle_sleep_setting_post()

        save_setting.assert_called_once_with(enabled=True)
        handler.redirect_home.assert_called_once_with("settings", anchor="night-mode")

    def test_sleep_timer_minutes_post_redirects_home_after_save(self) -> None:
        body = b"minutes=45"
        handler = object.__new__(SetupHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.redirect_home = Mock()

        with patch("abs_kids_player.setup_server.save_sleep_timer_setting") as save_setting:
            handler.handle_sleep_setting_post()

        save_setting.assert_called_once_with(minutes=45)
        handler.redirect_home.assert_called_once_with("settings", anchor="night-mode")

    def test_podcasts_post_redirects_home_after_save(self) -> None:
        body = b"podcast_url=https%3A%2F%2Fpodcasts.apple.com%2Fus%2Fpodcast%2Fexample%2Fid123"
        handler = object.__new__(SetupHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.redirect_home = Mock()

        with patch("abs_kids_player.setup_server.save_podcast_settings") as save_podcasts:
            handler.handle_podcasts_post()

        save_podcasts.assert_called_once_with(["https://podcasts.apple.com/us/podcast/example/id123"])
        handler.redirect_home.assert_called_once_with("podcasts")

    def test_system_reboot_post_responds_before_scheduling_reboot(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.send_page = Mock()

        with patch("abs_kids_player.setup_server.schedule_system_reboot") as schedule_reboot:
            handler.handle_system_reboot_post()

        handler.send_page.assert_called_once()
        schedule_reboot.assert_called_once_with()

    def test_system_update_post_pauses_playback_and_schedules_update(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.send_page = Mock()
        handler.post_fields = Mock(return_value={"version": ["0.4.0"]})

        with (
            patch("abs_kids_player.setup_server.queue_web_player_command") as queue_command,
            patch("abs_kids_player.setup_server.load_update_state", return_value=SoftwareUpdateState()),
            patch("abs_kids_player.setup_server.save_update_state") as save_state,
            patch("abs_kids_player.setup_server.schedule_software_update") as schedule_update,
        ):
            handler.handle_system_update_post()

        queue_command.assert_called_once_with("pause")
        state = save_state.call_args.args[0]
        self.assertEqual(state.phase, "installing")
        self.assertEqual(state.target_version, "0.4.0")
        self.assertEqual(state.message, "Preparing to install v0.4.0.")
        self.assertGreater(state.updated_at, 0)
        handler.send_page.assert_called_once()
        schedule_update.assert_called_once_with()

    def test_system_update_requires_a_valid_reviewed_version(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.send_request_error = Mock()
        handler.post_fields = Mock(return_value={"version": ["main"]})
        with (
            patch("abs_kids_player.setup_server.load_update_state", return_value=SoftwareUpdateState()),
            patch("abs_kids_player.setup_server.schedule_software_update") as schedule_update,
        ):
            handler.handle_system_update_post()
        handler.send_request_error.assert_called_once_with(400, "Review the available release before installing it.")
        schedule_update.assert_not_called()

    def test_system_update_cannot_be_requested_again_during_verification(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.send_request_error = Mock()
        state = SoftwareUpdateState(phase="verifying", updated_at=time.time())
        with (
            patch("abs_kids_player.setup_server.load_update_state", return_value=state),
            patch("abs_kids_player.setup_server.schedule_software_update") as schedule_update,
        ):
            handler.handle_system_update_post()
        handler.send_request_error.assert_called_once_with(409, "A software update is already running.")
        schedule_update.assert_not_called()

    def test_permanent_local_package_reports_its_source_and_keeps_online_updates_off(self):
        handler = object.__new__(SetupHandler)
        handler.send_json, handler.send_request_error = Mock(), Mock()
        with (
            patch.dict(os.environ, {"CHAPTER_RELEASE_SOURCE": "local"}, clear=True),
            patch("abs_kids_player.setup_server.schedule_software_update") as schedule_update,
            patch("abs_kids_player.setup_server.update_status_payload") as update_status,
        ):
            handler.send_system_update_status_json()
            handler.handle_system_update_post()
        handler.send_json.assert_called_once_with({
            "phase": "local", "updateAvailable": False, "message": "Installed from a local package.",
        })
        handler.send_request_error.assert_called_once_with(
            409, "This release is installed locally. Use the local release installer."
        )
        schedule_update.assert_not_called()
        update_status.assert_not_called()

    def test_system_update_status_get_returns_json_payload(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.send_json = Mock()

        with patch(
            "abs_kids_player.setup_server.update_status_payload",
            return_value={"phase": "current", "updateAvailable": False},
        ) as status_payload:
            handler.send_system_update_status_json()

        status_payload.assert_called_once_with()
        handler.send_json.assert_called_once_with(
            {"phase": "current", "updateAvailable": False}
        )

    def test_system_reset_post_restores_defaults_and_schedules_setup_mode(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.send_page = Mock()

        with (
            patch("abs_kids_player.setup_server.restore_device_defaults") as restore_defaults,
            patch("abs_kids_player.setup_server.schedule_system_reset") as schedule_reset,
        ):
            handler.handle_system_reset_post()

        restore_defaults.assert_called_once_with()
        handler.send_page.assert_called_once()
        schedule_reset.assert_called_once_with()

    def test_redirect_home_sends_empty_303_response(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler.redirect_home()

        handler.send_response.assert_called_once_with(303)
        handler.send_header.assert_any_call("Location", "/")
        handler.send_header.assert_any_call("Content-Length", "0")
        handler.end_headers.assert_called_once_with()

    def test_rendered_admin_page_protects_every_post_form_with_csrf_token(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.server = SimpleNamespace(csrf_token="test-csrf-token")
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()

        with (
            patch("abs_kids_player.setup_server.load_config", return_value=AppConfig()),
            patch(
                "abs_kids_player.setup_server.wifi_status",
                return_value=WifiStatus(connected=True, ssid="Chapter WiFi"),
            ),
            patch("abs_kids_player.setup_server.load_web_player_status", return_value=WebPlayerStatus()),
            patch("abs_kids_player.setup_server.bluetooth_status", return_value=(False, "Not connected")),
        ):
            handler.send_page()

        page = handler.wfile.getvalue().decode("utf-8")
        forms = re.findall(r'<form\b[^>]*method="post"[^>]*>(.*?)</form>', page, flags=re.DOTALL)
        self.assertGreater(len(forms), 0)
        for form in forms:
            self.assertIn('name="csrf_token" value="test-csrf-token"', form)
        self.assertIn('meta name="chapter-csrf-token" content="test-csrf-token"', page)
        self.assertIn('href="/settings/themes"', page)

    def test_themes_page_offers_eight_palettes_with_taupe_and_gold_as_default(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.path = "/settings/themes"
        handler.send_html = Mock()
        with patch("abs_kids_player.setup_server.owner_sessions.access") as access:
            access.return_value.can_manage = True
            handler.do_GET()

        status, page = handler.send_html.call_args.args
        self.assertEqual(status, 200)
        self.assertIn('<html lang="en" data-palette="taupe-gold"', page)
        self.assertIn('name="palette" value="taupe-gold" checked', page)
        self.assertIn('href="/?tab=settings">All settings</a>', page)
        self.assertEqual(page.count('type="radio" name="palette"'), 8)
        for name in ("Taupe &amp; Gold", "Tangerine &amp; Charcoal", "Midnight Brass"):
            self.assertIn(name, page)
        for name in ("JH Taupe", "CompleteSeries Orange", "Rail Deck Signal"):
            self.assertNotIn(name, page)

    def test_post_dispatch_accepts_valid_csrf_token(self) -> None:
        body = b"csrf_token=test-csrf-token"
        handler = object.__new__(SetupHandler)
        handler.path = "/system/reset"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.server = SimpleNamespace(csrf_token="test-csrf-token")
        handler.handle_system_reset_post = Mock()
        handler.send_csrf_error = Mock()

        handler.do_POST()

        handler.handle_system_reset_post.assert_called_once_with()
        handler.send_csrf_error.assert_not_called()

    def test_post_dispatch_rejects_missing_or_invalid_csrf_token(self) -> None:
        for body in (b"", b"csrf_token=wrong-token", b"csrf_token=%C3%A9"):
            with self.subTest(body=body):
                handler = object.__new__(SetupHandler)
                handler.path = "/system/reset"
                handler.headers = {"Content-Length": str(len(body))}
                handler.rfile = io.BytesIO(body)
                handler.server = SimpleNamespace(csrf_token="test-csrf-token")
                handler.handle_system_reset_post = Mock()
                handler.send_csrf_error = Mock()

                handler.do_POST()

                handler.handle_system_reset_post.assert_not_called()
                handler.send_csrf_error.assert_called_once_with()

    def test_post_body_size_is_limited_before_reading(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.headers = {"Content-Length": str(MAX_POST_BODY_BYTES + 1)}
        handler.rfile = io.BytesIO()
        handler.send_request_error = Mock()

        self.assertIsNone(handler.post_fields())

        handler.send_request_error.assert_called_once_with(413, "Request is too large.")

    def test_logo_asset_is_served_as_cached_png(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()

        handler.send_logo_png()

        body = handler.wfile.getvalue()
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_any_call("Content-Type", "image/png")
        handler.send_header.assert_any_call("Cache-Control", "public, max-age=86400")
        handler.send_header.assert_any_call("Content-Length", str(len(body)))
        handler.end_headers.assert_called_once_with()

    def test_wifi_forget_is_scheduled_after_five_second_response_window(self) -> None:
        timers = []

        class FakeTimer:
            def __init__(self, delay: float, callback) -> None:
                self.delay = delay
                self.callback = callback
                self.daemon = False
                self.started = False
                timers.append(self)

            def start(self) -> None:
                self.started = True

        with patch("abs_kids_player.setup_server.threading.Timer", FakeTimer):
            schedule_wifi_forget()

        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0].delay, WIFI_CHANGE_DELAY_SECONDS)
        self.assertTrue(timers[0].daemon)
        self.assertTrue(timers[0].started)

    def test_system_reboot_is_scheduled_after_response_window(self) -> None:
        timers = []
        commands = []

        class FakeTimer:
            def __init__(self, delay: float, callback) -> None:
                self.delay = delay
                self.callback = callback
                self.daemon = False
                timers.append(self)

            def start(self) -> None:
                pass

        with patch("abs_kids_player.setup_server.threading.Timer", FakeTimer):
            schedule_system_reboot(runner=lambda command: commands.append(command))

        self.assertEqual(timers[0].delay, SYSTEM_ACTION_DELAY_SECONDS)
        self.assertTrue(timers[0].daemon)
        timers[0].callback()
        self.assertEqual(commands, [["systemctl", "reboot"]])

    def test_software_update_is_started_after_response_window(self) -> None:
        timers = []
        commands = []

        class FakeTimer:
            def __init__(self, delay: float, callback) -> None:
                self.delay = delay
                self.callback = callback
                self.daemon = False
                timers.append(self)

            def start(self) -> None:
                pass

        wait_for_display = Mock(return_value=True)
        with patch("abs_kids_player.setup_server.threading.Timer", FakeTimer):
            schedule_software_update(
                runner=lambda command: commands.append(command),
                display_ready_waiter=wait_for_display,
            )

        self.assertEqual(timers[0].delay, SYSTEM_ACTION_DELAY_SECONDS)
        self.assertTrue(timers[0].daemon)
        timers[0].callback()
        wait_for_display.assert_called_once_with()
        self.assertEqual(
            commands,
            [["systemctl", "start", "--no-block", "audiobookshelf-player-update.service"]],
        )

    def test_system_reset_is_scheduled_after_response_window(self) -> None:
        timers = []

        class FakeTimer:
            def __init__(self, delay: float, callback) -> None:
                self.delay = delay
                self.callback = callback
                self.daemon = False
                timers.append(self)

            def start(self) -> None:
                pass

        with (
            patch("abs_kids_player.setup_server.threading.Timer", FakeTimer),
            patch(
                "abs_kids_player.setup_server.forget_all_wifi_connections_and_start_hotspot"
            ) as forget_wifi,
        ):
            schedule_system_reset()
            timers[0].callback()

        self.assertEqual(timers[0].delay, WIFI_CHANGE_DELAY_SECONDS)
        self.assertTrue(timers[0].daemon)
        forget_wifi.assert_called_once_with()

    def test_restore_device_defaults_clears_player_data_and_refreshes_library(self) -> None:
        with (
            patch("abs_kids_player.setup_server.save_config") as save_config,
            patch("abs_kids_player.setup_server.clear_cached_books") as clear_cache,
            patch("abs_kids_player.setup_server.clear_last_playback") as clear_playback,
            patch("abs_kids_player.setup_server.queue_web_player_command") as queue_command,
        ):
            config = restore_device_defaults()

        self.assertFalse(config.is_ready)
        self.assertTrue(config.control_click_enabled)
        self.assertFalse(config.sleep_timer_enabled)
        save_config.assert_called_once_with(config)
        clear_cache.assert_called_once_with()
        clear_playback.assert_called_once_with()
        queue_command.assert_called_once_with("refresh_library")

    def test_login_username_prefers_server_response(self) -> None:
        self.assertEqual(login_username({"user": {"username": "chapter"}}, "fallback"), "chapter")
        self.assertEqual(login_username({"user": {}}, "fallback"), "fallback")

    def test_login_and_refresh_player_saves_new_user_library_and_queues_refresh(self) -> None:
        write_settings(AppConfig(control_click_enabled=False, screen_saver_mode=SCREEN_SAVER_BOOKS))
        with (
            patch("abs_kids_player.setup_server.AudiobookshelfClient", FakeAudiobookshelfClient),
            patch("abs_kids_player.setup_server.queue_web_player_command") as queue_command,
        ):
            config = login_and_refresh_player("https://books.example.com", "chapter", "password")

        self.assertEqual(config.server_url, "https://books.example.com")
        self.assertEqual(config.refresh_token, "refresh-token")
        self.assertEqual(config.library_id, "library-1")
        self.assertEqual(config.username, "chapter")
        self.assertFalse(config.control_click_enabled)
        self.assertEqual(config.screen_saver_mode, SCREEN_SAVER_BOOKS)
        self.assertEqual(read_settings(), config)
        self.assertEqual(
            [call.args for call in queue_command.call_args_list],
            [("pause",), ("refresh_library",)],
        )

if __name__ == "__main__":
    unittest.main()
