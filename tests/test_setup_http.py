import errno
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from http.client import HTTPConnection
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from abs_kids_player.config import AppConfig, config_path, load_config, save_config
from abs_kids_player.library_cache import save_cached_books
from abs_kids_player.models import Book
from abs_kids_player.owner_auth import (
    OwnerSessions,
    change_public_access,
    load_credential,
    set_password,
)
from abs_kids_player.setup_server import SetupHandler
from abs_kids_player.web_device_status import WifiStatusCache
from abs_kids_player.wifi import WifiStatus


class SetupRequestRecoveryTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "ABS_KIDS_PLAYER_CONFIG_PATH": directory + "/config.json",
            "ABS_KIDS_PLAYER_STATE_DIR": directory,
            "CHAPTER_OWNER_FILE": directory + "/owner.json",
        }))
        self.server = SimpleNamespace(csrf_token="test-form")

    @contextmanager
    def local_connection(self):
        browser, server_socket = socket.socketpair()
        def serve():
            with server_socket:
                server_socket.settimeout(2)
                SetupHandler(server_socket, ("local", 0), self.server)

        with browser, ThreadPoolExecutor(1) as worker, closing(HTTPConnection("chapter.test")) as connection:
            browser.settimeout(2)
            connection.sock = browser
            exchange = worker.submit(serve)
            try:
                yield connection
            finally:
                connection.close()
                exchange.result(timeout=2)

    def request(self, method, path, *, fields=None, headers=None, end_upload=False):
        with self.local_connection() as connection:
            connection.request(method, path, body=urlencode(fields) if fields is not None else None,
                               headers={"Connection": "close", **(headers or {})})
            if end_upload:
                connection.sock.shutdown(socket.SHUT_WR)
            with connection.getresponse() as reply:
                return reply.status, reply.headers, reply.read()

    def test_downloads_remain_read_only_for_visitors_while_an_owner_can_queue_and_cancel(self):
        config = AppConfig(server_url="http://books.invalid", username="reader", token="fixture", library_id="library")
        save_config(config)
        save_cached_books(config, [Book("river", "The River", "A Writer", 60, "")])
        self.enterContext(patch("abs_kids_player.setup_server.owner_sessions", OwnerSessions()))
        set_password("test owner password", "reader")
        change_public_access(load_credential(), overview=False, library=True)
        fetch = {"X-Requested-With": "fetch"}
        action = {"csrf_token": self.server.csrf_token, "book_id": "river", "action": "download"}
        target = "/library/download/status?section=downloads&id=river"

        status, _, _ = self.request("POST", "/library/download", fields=action, headers=fetch)
        self.assertEqual(status, 401)
        status, headers, _ = self.request("POST", "/owner/sign-in", fields={
            "csrf_token": self.server.csrf_token, "username": "reader", "password": "test owner password",
        })
        self.assertEqual(status, 303)
        owner = {**fetch, "Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        status, _, body = self.request("POST", "/library/download", fields=action, headers=owner)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["card"]["label"], "Queued")

        status, _, body = self.request("GET", target, headers=fetch)
        self.assertEqual(status, 200)
        guest = json.loads(body)
        self.assertEqual(guest["cards"]["river"]["label"], "Queued")
        self.assertEqual(guest["cards"]["river"]["buttons"], "")
        self.assertNotIn("<form", guest["queue"])
        self.assertNotIn("csrf", guest)
        self.assertNotIn("player", guest)
        status, _, body = self.request("GET", target, headers=owner)
        self.assertEqual(status, 200)
        self.assertIn('value="cancel"', json.loads(body)["cards"]["river"]["buttons"])
        status, _, _ = self.request("POST", "/library/download", fields={**action, "action": "cancel"}, headers=owner)
        self.assertEqual(status, 200)
        status, _, body = self.request("GET", target, headers=fetch)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["queueHeading"], "Download queue (0)")

    def test_damaged_settings_explain_recovery_and_accept_a_repaired_file(self):
        config_path().write_bytes(b"[]")
        status, _, body = self.request("GET", "/settings/display")
        self.assertEqual(status, 503)
        self.assertIn(b"restore a known-good copy", body)
        self.assertEqual(config_path().read_bytes(), b"[]")
        config_path().write_text("{}")
        self.assertEqual(self.request("GET", "/settings/display")[0], 200)

    def test_dashboard_and_health_respond_while_one_wifi_check_serves_both(self):
        started, release = threading.Event(), threading.Event()
        def slow_probe():
            started.set()
            release.wait(3)
            return WifiStatus(True, ssid="Reading & Music")

        with (patch("abs_kids_player.setup_server.web_wifi", WifiStatusCache()),
              patch("abs_kids_player.setup_server.wifi_status", side_effect=slow_probe) as probe,
              patch("abs_kids_player.setup_server.download_activity_status", return_value={}),
              patch("abs_kids_player.setup_server.web_bluetooth.current", return_value=(False, "")),
              ThreadPoolExecutor(2) as clients):
            try:
                dashboard = clients.submit(self.request, "GET", "/?tab=connections")
                self.assertTrue(started.wait(1))
                health = clients.submit(self.request, "GET", "/health")
                for response in (dashboard, health):
                    status, _, body = response.result(timeout=1)
                    self.assertEqual(status, 200)
                    self.assertIn("Checking Wi-Fi…".encode(), body)
                probe.assert_called_once()
            finally:
                release.set()
            deadline = time.monotonic() + 2
            while True:
                data = json.loads(self.request("GET", "/status/wifi?card=1")[2])
                if data["connected"] is not None or time.monotonic() >= deadline:
                    break
            self.assertTrue(data["connected"])
            self.assertIn("Reading &amp; Music", data["label"])
            self.assertIn('action="/wifi/forget"', data["card"])
            self.assertIn("test-form", data["card"])
            self.assertNotIn("card", json.loads(self.request("GET", "/status/wifi")[2]))
            probe.assert_called_once()

    def test_visitor_overview_does_not_expose_wifi_details(self):
        access = SimpleNamespace(can_manage=False, public_sections=("overview",))
        with (patch("abs_kids_player.setup_server.owner_sessions.access", return_value=access),
              patch("abs_kids_player.setup_server.web_wifi.current") as probe):
            status, _, _ = self.request("GET", "/status/wifi?card=1", headers={"X-Requested-With": "fetch"})
            self.assertEqual(status, 401)
            probe.assert_not_called()

    def test_failed_settings_save_explains_the_problem_and_the_next_save_succeeds(self):
        save_config(AppConfig(screen_saver_dim_percent=10))
        fields = {"csrf_token": "test-form", "screen_saver_dim_percent": "5"}
        with patch("abs_kids_player.config.write_json_atomically", side_effect=OSError(errno.ENOSPC, "test path")):
            status, headers, body = self.request("POST", "/settings/display", fields=fields)
        self.assertEqual(status, 503)
        self.assertEqual(headers.get_content_type(), "text/html")
        self.assertIn(b"out of storage space", body)
        self.assertNotIn(b"test path", body)
        self.assertEqual(load_config().screen_saver_dim_percent, 10)

        status, headers, _ = self.request("POST", "/settings/display", fields=fields)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/settings/display?saved=1")
        self.assertEqual(load_config().screen_saver_dim_percent, 5)

    def test_interrupted_display_form_preserves_settings_and_accepts_a_retry(self):
        save_config(AppConfig(screen_off_minutes=15))
        fields = {"csrf_token": "test-form", "screen_off_minutes": "1"}
        status, headers, body = self.request("POST", "/settings/display", fields=fields,
                                            headers={"Content-Length": str(len(urlencode(fields)) + 10),
                                                     "Connection": "keep-alive"}, end_upload=True)
        self.assertEqual(status, 400)
        self.assertEqual(headers["Connection"], "close")
        self.assertIn(b"form was interrupted", body)
        self.assertEqual(load_config().screen_off_minutes, 15)
        self.assertEqual(self.request("POST", "/settings/display", fields=fields)[0], 303)
        self.assertEqual(load_config().screen_off_minutes, 1)

    def test_unknown_settings_system_and_player_pages_return_not_found(self):
        for section in ("settings", "system", "player"):
            with self.subTest(section=section):
                self.assertEqual(self.request("GET", f"/{section}/unknown")[0], 404)

    def test_two_settings_saves_on_one_connection_apply_each_form(self):
        with self.local_connection() as connection:
            for minutes, header in ((1, "keep-alive"), (15, "close")):
                form = urlencode({"csrf_token": "test-form", "screen_off_minutes": minutes})
                connection.request("POST", "/settings/display", body=form, headers={"Connection": header})
                with connection.getresponse() as response:
                    self.assertEqual(response.status, 303)
                    response.read()
                self.assertEqual(load_config().screen_off_minutes, minutes)

    def test_player_fetch_gets_an_error_then_a_normal_status(self):
        with patch("abs_kids_player.setup_server.load_web_player_status", side_effect=OSError("private path")):
            status, headers, body = self.request("GET", "/player/status", headers={"X-Requested-With": "fetch"})
        self.assertEqual(status, 503)
        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertIn("error", json.loads(body))
        self.assertNotIn(b"private path", body)
        status, _, body = self.request("GET", "/player/status")
        self.assertEqual(status, 200)
        self.assertIn("isPlaying", json.loads(body))

    def test_unexpected_page_failure_is_logged_and_the_next_request_works(self):
        with (patch("abs_kids_player.setup_server.render_settings_page", side_effect=RuntimeError("renderer failed")),
              self.assertLogs("abs_kids_player.setup_http", level="ERROR") as log):
            status, _, body = self.request("GET", "/settings/display")
        self.assertEqual(status, 500)
        self.assertNotIn(b"renderer failed", body)
        self.assertIn("renderer failed", log.output[0])
        self.assertEqual(self.request("GET", "/player/status")[0], 200)

    def test_interrupted_status_stream_ends_without_appending_an_error_page(self):
        with patch("abs_kids_player.setup_server.load_web_player_status", side_effect=OSError("read failed")):
            status, headers, body = self.request("GET", "/player/events")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "text/event-stream")
        self.assertEqual(body, b"retry: 2000\n\n")
        self.assertEqual(self.request("GET", "/player/status")[0], 200)

    def test_error_replaces_only_headers_that_have_not_been_sent(self):
        def interrupted(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "image/png")
            raise OSError("image read failed")

        with patch.object(SetupHandler, "send_logo_png", interrupted):
            status, headers, body = self.request("GET", "/assets/chapter-logo.png")
        self.assertEqual(status, 503)
        self.assertEqual(headers.get_all("Content-Type"), ["text/html; charset=utf-8"])
        self.assertEqual(len(body), int(headers["Content-Length"]))
