from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from abs_kids_player.catalogue_settings import (
    CATALOGUE_COMMAND,
    LibraryOption,
    LibraryUnavailable,
    available_book_libraries,
    catalogue_request_key,
    load_catalogue_status,
    report_catalogue_result,
    select_library,
)
from abs_kids_player.config import AppConfig
from abs_kids_player.setup_server import SetupHandler
from abs_kids_player.setup_views import CSRF_FIELD_NAME


class HTTPRequest:
    def __init__(self, request: str) -> None:
        self.input = io.BytesIO(request.encode())
        self.output = io.BytesIO()

    def makefile(self, mode: str, _buffer_size: int):
        if mode != "rb":
            raise AssertionError(mode)
        return self.input

    def sendall(self, data: bytes) -> None:
        self.output.write(data)


class CatalogueSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(
            server_url="https://books.example.test", token="token", username="reader",
            library_id="first", library_name="First library", pinned_shelf="collections/pinned",
        )
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict("os.environ", {
            "ABS_KIDS_PLAYER_LIBRARY_CACHE_PATH": str(directory / "catalogue.json"),
            "ABS_KIDS_PLAYER_WEB_COMMANDS_PATH": str(directory / "commands.json"),
        }))

    def test_picker_lists_accessible_audiobook_libraries_and_preserves_other_settings(self) -> None:
        client = Mock()
        client.get_libraries.return_value = [
            {"id": "podcasts", "name": "Podcasts", "mediaType": "podcast"},
            {"id": "first", "name": "First library", "mediaType": "book"},
            {"id": "children", "name": "Children's books", "mediaType": "book"},
        ]
        with patch("abs_kids_player.catalogue_settings.client_from_config", return_value=client):
            self.assertEqual(available_book_libraries(self.config), [
                LibraryOption("children", "Children's books"), LibraryOption("first", "First library"),
            ])
            same = select_library(self.config, "first")
            switched = select_library(self.config, "children")
            with self.assertRaises(LibraryUnavailable):
                select_library(self.config, "podcasts")

        self.assertEqual(same.pinned_shelf, self.config.pinned_shelf)
        self.assertEqual((switched.library_id, switched.library_name, switched.pinned_shelf),
                         ("children", "Children's books", ""))
        self.assertEqual((switched.token, switched.username), (self.config.token, self.config.username))

    def test_failed_refresh_retains_last_success_time(self) -> None:
        with patch("abs_kids_player.catalogue_settings.time.time", return_value=5000):
            report_catalogue_result(self.config)
        report_catalogue_result(self.config, "Server unreachable")
        status = load_catalogue_status(self.config)
        self.assertEqual((status.error, status.last_success_at), ("Server unreachable", 5000))
        self.assertEqual(load_catalogue_status(AppConfig()).last_success_at, 0)

    def request(self, method: str, path: str, fields: dict | None = None) -> str:
        body = urlencode(fields or {})
        request = HTTPRequest(
            f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
            f"Content-Length: {len(body)}\r\nContent-Type: application/x-www-form-urlencoded\r\n\r\n{body}"
        )
        with (
            patch("abs_kids_player.setup_server.load_config", return_value=self.config),
            patch.object(SetupHandler, "log_message"),
        ):
            SetupHandler(request, ("127.0.0.1", 12345), SimpleNamespace(csrf_token="csrf"))
        return request.output.getvalue().decode()

    def test_catalogue_page_opens_without_contacting_audiobookshelf(self) -> None:
        with patch("abs_kids_player.setup_server.available_book_libraries") as load:
            response = self.request("GET", "/settings/catalogue")
        load.assert_not_called()
        self.assertIn("200 OK", response)
        self.assertIn("First library", response)
        self.assertIn("Refresh library", response)
        self.assertIn("Last successful refresh", response)

    def test_real_http_post_queues_selection_without_saving_it_immediately(self) -> None:
        from abs_kids_player.player_control import consume_web_player_commands

        fields = {
            CSRF_FIELD_NAME: "csrf", "action": "select",
            "catalogue_key": catalogue_request_key(self.config), "library_id": "children",
        }
        with patch("abs_kids_player.setup_server.save_config") as save:
            response = self.request("POST", "/settings/catalogue", fields)
        save.assert_not_called()
        queued, = consume_web_player_commands()
        self.assertEqual((queued.action, queued.library_id), (CATALOGUE_COMMAND, "children"))
        self.assertIn(f"Location: /settings/catalogue?request={queued.id}", response)
        self.assertIn("303 See Other", response)

    def test_real_http_post_rejects_stale_settings_and_missing_csrf(self) -> None:
        with patch("abs_kids_player.setup_server.queue_web_player_command") as enqueue:
            self.assertIn("403 Forbidden", self.request("POST", "/settings/catalogue", {"action": "refresh"}))
            self.assertIn("409 Conflict", self.request("POST", "/settings/catalogue", {
                "action": "refresh", CSRF_FIELD_NAME: "csrf", "catalogue_key": "old-key",
            }))
        enqueue.assert_not_called()
