from __future__ import annotations

import io
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from abs_kids_player.config import AppConfig, load_config, save_config
from abs_kids_player.connection_health import (
    ConnectionHealth,
    ConnectionMonitor,
    ConnectionState,
    check_connection,
)


class ConnectionHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(
            server_url="https://books.example.com/audiobookshelf",
            token="token",
            username="saved-user",
            library_id="library-1",
        )

    def test_saved_login_is_verified_with_user_and_library_requests(self) -> None:
        with patch(
            "abs_kids_player.api.urlopen",
            side_effect=[json_response({"username": "verified-user"}), json_response({"libraries": [{"id": "library-1"}]})],
        ) as request:
            status = check_connection(self.config)

        self.assertEqual(status, ConnectionHealth(ConnectionState.CONNECTED, "verified-user"))
        self.assertEqual(
            [call.args[0].full_url for call in request.call_args_list],
            [
                "https://books.example.com/audiobookshelf/api/me",
                "https://books.example.com/audiobookshelf/api/libraries",
            ],
        )
        self.assertTrue(all(call.kwargs["timeout"] > 0 for call in request.call_args_list))

    def test_failed_verification_classifies_http_and_network_errors(self) -> None:
        errors = (
            (URLError("offline"), ConnectionState.UNREACHABLE),
            (TimeoutError(), ConnectionState.TIMED_OUT),
            (URLError(TimeoutError()), ConnectionState.TIMED_OUT),
            (http_error(401), ConnectionState.SIGN_IN_REQUIRED),
            (http_error(403), ConnectionState.SIGN_IN_REQUIRED),
            (http_error(503), ConnectionState.SERVER_ERROR),
        )
        for error, expected in errors:
            with (
                self.subTest(state=expected),
                patch("abs_kids_player.api.urlopen", side_effect=error),
            ):
                self.assertEqual(check_connection(self.config), ConnectionHealth(expected))

    def test_a_valid_account_with_an_inaccessible_library_stays_unhealthy(self) -> None:
        for libraries in (json_response([{"id": "different-library"}]), http_error(403), http_error(404)):
            with (
                self.subTest(library_response=libraries),
                patch("abs_kids_player.api.urlopen", side_effect=[json_response({"username": "listener"}), libraries]),
            ):
                status = check_connection(self.config)
                self.assertEqual(status, ConnectionHealth(ConnectionState.LIBRARY_UNAVAILABLE, "listener"))

    def test_non_audiobookshelf_responses_cannot_be_healthy(self) -> None:
        for body in (b"<html>Wrong site</html>", b'{"unrelated": "object"}', b"\xff"):
            with (
                self.subTest(body=body),
                patch("abs_kids_player.api.urlopen", return_value=io.BytesIO(body)),
            ):
                self.assertEqual(check_connection(self.config).state, ConnectionState.SERVER_ERROR)

        with patch(
            "abs_kids_player.api.urlopen",
            side_effect=[json_response({"username": "listener"}), io.BytesIO(b"<html>Wrong site</html>")],
        ):
            self.assertEqual(check_connection(self.config).state, ConnectionState.SERVER_ERROR)

    def test_persists_verified_username_for_old_configs_without_overwriting_a_new_login(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"ABS_KIDS_PLAYER_CONFIG_PATH": str(Path(directory) / "config.json")}):
                config = replace(self.config, username="unknown user")
                save_config(config)
                with patch(
                    "abs_kids_player.api.urlopen",
                    side_effect=[json_response({"username": "listener"}), json_response([{"id": "library-1"}])],
                ):
                    check_connection(config)
                self.assertEqual(load_config().username, "listener")

                save_config(replace(config, server_url="https://new.example.test", token="new-token"))
                with patch(
                    "abs_kids_player.api.urlopen",
                    side_effect=[json_response({"username": "old-listener"}), json_response([{"id": "library-1"}])],
                ):
                    check_connection(config)
                self.assertEqual(load_config().username, "unknown user")
                self.assertEqual(load_config().token, "new-token")

    def test_monitor_caches_a_result_and_recovers_after_a_failed_check(self) -> None:
        now = Mock(return_value=100.0)
        check = Mock(side_effect=[
            ConnectionHealth(ConnectionState.UNREACHABLE),
            ConnectionHealth(ConnectionState.CONNECTED, "listener"),
        ])
        worker = Mock()
        monitor = ConnectionMonitor(check, clock=now)
        with patch("abs_kids_player.connection_health.threading.Thread", worker):
            self.assertEqual(monitor.status_for(AppConfig()).state, ConnectionState.NOT_CONFIGURED)
            worker.assert_not_called()
            self.assertEqual(monitor.status_for(self.config).state, ConnectionState.CHECKING)
            self.assertEqual(monitor.status_for(self.config).state, ConnectionState.CHECKING)
            worker.assert_called_once()
            run_worker(worker)

            status = monitor.status_for(self.config)
            self.assertEqual(status.state, ConnectionState.UNREACHABLE)
            self.assertIsNotNone(status.checked_at)
            worker.assert_called_once()

            now.return_value = 131.0
            self.assertEqual(monitor.status_for(self.config), status)
            self.assertEqual(worker.call_count, 2)
            run_worker(worker)
            self.assertEqual(monitor.status_for(self.config).state, ConnectionState.CONNECTED)

    def test_monitor_discards_results_for_superseded_credentials(self) -> None:
        check = Mock(side_effect=[
            ConnectionHealth(ConnectionState.CONNECTED, "old-user"),
            ConnectionHealth(ConnectionState.LIBRARY_UNAVAILABLE, "new-user"),
        ])
        worker = Mock()
        monitor = ConnectionMonitor(check)
        updated = replace(self.config, token="replacement")
        with patch("abs_kids_player.connection_health.threading.Thread", worker):
            monitor.status_for(self.config)
            self.assertEqual(monitor.status_for(updated).state, ConnectionState.CHECKING)
            run_worker(worker)

            self.assertEqual(monitor.status_for(updated).state, ConnectionState.CHECKING)
            run_worker(worker)
            self.assertEqual(monitor.status_for(updated).username, "new-user")

    def test_monitor_retries_when_a_check_cannot_start(self):
        now = Mock(return_value=10.0)
        monitor = ConnectionMonitor(lambda _: ConnectionHealth(ConnectionState.CONNECTED), clock=now)
        with patch("abs_kids_player.connection_health.threading.Thread") as worker:
            worker.return_value.start.side_effect = [RuntimeError("thread unavailable"), None]
            self.assertEqual(monitor.status_for(self.config).state, ConnectionState.CHECK_UNAVAILABLE)
            self.assertEqual(monitor.status_for(self.config).state, ConnectionState.CHECK_UNAVAILABLE)
            worker.assert_called_once()
            now.return_value += 31
            monitor.status_for(self.config)
            run_worker(worker)
            self.assertEqual(monitor.status_for(self.config).state, ConnectionState.CONNECTED)


def json_response(value: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(value).encode("utf-8"))


def http_error(code: int) -> HTTPError:
    return HTTPError("https://books.example.com/", code, "", {}, io.BytesIO(b"private details"))


def run_worker(worker: Mock) -> None:
    options = worker.call_args.kwargs
    options["target"](*options["args"])
