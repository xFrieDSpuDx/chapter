import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from abs_kids_player.config import AppConfig
from abs_kids_player.owner_auth import (
    COOKIE_NAME,
    OwnerSessions,
    change_public_access,
    load_credential,
    set_password,
)
from abs_kids_player.player_control import WebPlayerStatus
from abs_kids_player.setup_server import SetupHandler


class OwnerAccessTest(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / "owner.json"
        self.enterContext(patch.dict("os.environ", {"CHAPTER_OWNER_FILE": str(self.path)}))
        self.sessions = OwnerSessions()
        self.enterContext(patch("abs_kids_player.setup_server.owner_sessions", self.sessions))

    def test_credentials_can_be_recovered_on_device_and_revoke_existing_sessions(self):
        self.assertTrue(self.sessions.allows(""))
        password = "lamp reading room"
        set_password(password)
        self.assertNotIn(password, self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.sessions.allows(""))
        token = self.sessions.sign_in("admin", password)
        self.assertTrue(self.sessions.allows(f"{COOKIE_NAME}={token}"))
        set_password("new local password")
        self.assertFalse(self.sessions.allows(f"{COOKIE_NAME}={token}"))
        self.assertTrue(self.sessions.sign_in("admin", "new local password"))

    def test_wrong_password_is_rate_limited_and_corrupt_credentials_do_not_unlock_admin(self):
        set_password("right reading password")
        with patch("abs_kids_player.owner_auth.time.monotonic", return_value=20) as clock:
            self.assertFalse(self.sessions.sign_in("wrong-user", "right reading password"))
            self.assertFalse(self.sessions.sign_in("admin", "right reading password"))
            clock.return_value = 21
            token = self.sessions.sign_in("admin", "right reading password")
            self.assertTrue(token)
            clock.return_value += 12 * 60 * 60
            self.assertFalse(self.sessions.allows(f"{COOKIE_NAME}={token}"))
        self.path.write_text("{invalid")
        self.assertFalse(self.sessions.allows(""))

    def test_same_http_connection_checks_fresh_csrf_and_owner_cookie_before_system_operation(self):
        set_password("right reading password")
        handler = object.__new__(SetupHandler)
        handler.csrf_token = Mock(return_value="csrf")
        handler.handle_system_reboot_post = Mock()
        handler.send_owner_required = Mock()
        handler.send_csrf_error = Mock()
        handler.redirect_owner = Mock()

        def post(path, fields, cookie=""):
            data = urlencode(fields).encode()
            handler.path = path
            handler.headers = {"Cookie": cookie, "Content-Length": str(len(data))}
            handler.rfile = io.BytesIO(data)
            handler.do_POST()

        post("/system/reboot", {"csrf_token": "csrf"})
        handler.send_owner_required.assert_called_once()
        post("/owner/sign-in", {"csrf_token": "csrf", "username": "admin", "password": "right reading password"})
        cookie = handler.redirect_owner.call_args.kwargs["cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        cookie_header = cookie.split(";", 1)[0]

        post("/system/reboot", {"csrf_token": "invalid"}, cookie_header)
        handler.send_csrf_error.assert_called_once()
        handler.handle_system_reboot_post.assert_not_called()
        post("/system/reboot", {"csrf_token": "csrf"}, cookie_header)
        handler.handle_system_reboot_post.assert_called_once()
        post("/owner/sign-out", {"csrf_token": "csrf"}, cookie_header)
        self.assertFalse(self.sessions.allows(cookie_header))

    def test_unauthed_library_is_blocked_and_updater_probe_has_only_display_timestamp(self):
        set_password("right reading password")
        handler = object.__new__(SetupHandler)
        handler.path = "/library"
        handler.send_library = Mock()
        handler.send_owner_required = Mock()
        handler.send_json = Mock()
        handler.do_GET()
        handler.send_owner_required.assert_called_once()
        handler.send_library.assert_not_called()

        with patch("abs_kids_player.setup_server.load_web_player_status") as status:
            status.return_value.display_drawn_at = 5
            handler.path = "/player/status"
            handler.do_GET()
        handler.send_json.assert_called_once_with({"displayDrawnAt": 5})

    def test_password_only_install_uses_the_default_username_after_upgrade(self):
        set_password("right reading password")
        current = load_credential()
        self.path.write_text(json.dumps({"salt": current.salt, "digest": current.digest}))
        self.assertTrue(self.sessions.sign_in("admin", "right reading password"))
        self.assertEqual(self.sessions.access("").public_sections, ())

    def test_security_can_create_an_account_and_allow_only_selected_reads(self):
        handler = object.__new__(SetupHandler)
        handler.csrf_token = Mock(return_value="csrf")
        handler.send_html = Mock()
        handler.send_json = Mock()
        handler.redirect_settings = Mock()
        handler.redirect_owner = Mock()
        handler.send_owner_required = Mock()
        handler.send_library = Mock()
        handler.send_artwork = Mock()
        handler.handle_player_post = Mock()
        handler.handle_library_download = Mock()
        password = " right reading password "

        def get(path, cookie=""):
            handler.path = path
            handler.headers = {"Cookie": cookie}
            handler.do_GET()

        def post(path, cookie="", **fields):
            data = urlencode({"csrf_token": "csrf", **fields}).encode()
            handler.path = path
            handler.headers = {"Cookie": cookie, "Content-Length": str(len(data))}
            handler.rfile = io.BytesIO(data)
            handler.do_POST()

        get("/settings/security")
        self.assertIn("Require owner login", handler.send_html.call_args.args[1])
        post("/settings/security/account", username="reader", password=password, confirm_password=password)
        self.assertEqual(load_credential().username, "reader")
        self.assertEqual(self.sessions.access("").public_sections, ())
        post("/owner/sign-in", username="reader", password=password)
        cookie = handler.redirect_owner.call_args.kwargs["cookie"].split(";", 1)[0]
        self.assertTrue(self.sessions.allows(cookie))

        post("/settings/security/access", cookie, public_overview="1")
        handler.redirect_settings.assert_called_once_with("security")
        self.assertTrue(self.sessions.allows(cookie))
        self.assertEqual(self.sessions.access("").public_sections, ("overview",))
        with (
            patch("abs_kids_player.setup_server.load_config", return_value=AppConfig(night_mode="on")),
            patch("abs_kids_player.setup_server.load_web_player_status", return_value=WebPlayerStatus(title="Listening")),
            patch("abs_kids_player.setup_server.download_activity_status", return_value={"downloaded": "1 book downloaded"}),
        ):
            get("/status/overview")
        status = handler.send_json.call_args.args[0]
        self.assertEqual(status["nightMode"], "Active · No scheduled end")
        self.assertEqual(status["playingTitle"], "Listening")
        self.assertNotIn("coverUrl", status)
        for path in (
            "/?tab=system", "/settings/security", "/settings/themes", "/system/settings-backup",
            "/library", "/artwork/one", "/player/events",
        ):
            with self.subTest(path=path):
                handler.send_owner_required.reset_mock()
                get(path)
                handler.send_owner_required.assert_called_once()

        post("/settings/security/access", cookie, public_library="1")
        get("/library?downloaded=1")
        handler.send_library.assert_called_once_with({"downloaded": ["1"]}, guest_sections=("library", "downloads"))
        get("/artwork/one")
        handler.send_artwork.assert_called_once_with("one", full=False)
        for path in ("/player", "/library/download", "/settings/security/access"):
            with self.subTest(path=path):
                handler.send_owner_required.reset_mock()
                post(path)
                handler.send_owner_required.assert_called_once()
        handler.handle_player_post.assert_not_called()
        handler.handle_library_download.assert_not_called()

    def test_cli_recovery_keeps_visitor_settings_and_invalidates_the_old_login(self):
        set_password("right reading password", "reader")
        token = self.sessions.sign_in("reader", "right reading password")
        change_public_access(load_credential(), overview=True, library=False)
        set_password("new local password")
        self.assertEqual(self.sessions.access("").public_sections, ("overview",))
        self.assertFalse(self.sessions.allows(f"{COOKIE_NAME}={token}"))
        self.assertTrue(self.sessions.sign_in("reader", "new local password"))

    def test_open_player_event_stream_checks_authorization_before_sending_status(self):
        set_password("right reading password")
        handler = object.__new__(SetupHandler)
        handler.headers = {}
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        with patch("abs_kids_player.setup_server.load_web_player_status", return_value=WebPlayerStatus(title="Private book")):
            handler.send_player_events()
        self.assertEqual(handler.wfile.getvalue(), b"retry: 2000\n\n")
        self.assertTrue(handler.close_connection)
