from __future__ import annotations

import io
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from abs_kids_player.api import AudiobookshelfError
from abs_kids_player.catalogue_settings import catalogue_request_key
from abs_kids_player.config import AppConfig
from abs_kids_player.download_queue_store import DOWNLOADED, DOWNLOADING, QUEUED, WAITING
from abs_kids_player.download_store import account_directory, book_directory, read_download
from abs_kids_player.download_transfer import DISK_RESERVE_BYTES, DownloadError
from abs_kids_player.download_views import render_library
from abs_kids_player.downloads import Downloads
from abs_kids_player.models import Book, SeriesMembership
from abs_kids_player.oled_service import handle_web_book_request
from abs_kids_player.playback_owner import PlaybackOwner, PlaybackSnapshot
from abs_kids_player.playback_start import prepare_book_request
from abs_kids_player.player_control import consume_web_player_commands
from abs_kids_player.rotary_ui import ApplianceMenu, MenuCommand
from abs_kids_player.setup_server import SetupHandler, run_setup_server


class AudioResponse(io.BytesIO):
    def __init__(self, data: bytes, *, resumed: bool = False, disconnected: bool = False) -> None:
        super().__init__(data)
        self.status = 206 if resumed else 200
        self.headers = {"ETag": '"revision"'}
        if resumed:
            self.headers["Content-Range"] = "bytes 2-5/6"
        self.disconnected = disconnected

    def read(self, size: int = -1) -> bytes:
        data = super().read(size)
        if not data and self.disconnected:
            raise ConnectionResetError("Wi-Fi dropped")
        return data


class DownloadQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch("abs_kids_player.download_transfer.save_download_cover"))
        self.enterContext(patch("abs_kids_player.downloads.DownloadAssetBackfill.schedule"))
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {
            "ABS_KIDS_PLAYER_STATE_DIR": folder, "ABS_KIDS_PLAYER_STATE_PATH": f"{folder}/state.json",
        }))
        self.config = AppConfig(server_url="https://books.example.test", username="reader", token="secret",
                                library_id="lib")
        self.books = [
            Book("a", "First book", "Example Reader", 60, "https://example.test/cover?token=private",
                 series_memberships=(SeriesMembership("series", "Stories", "1"),)),
            Book("b", "Second book", "", 60, ""),
        ]
        self.now = 100
        self.queue = self.new_queue()
        self.client = Mock()
        self.client.request.return_value = {"media": {
            "tracks": [{"ino": "123", "duration": 60, "metadata": {"size": 6}}],
        }}
        self.client_factory = self.enterContext(
            patch("abs_kids_player.download_transfer.client_from_config", return_value=self.client)
        )
        self.disk = self.enterContext(
            patch("abs_kids_player.download_transfer.storage_usage", return_value=(0, 2 * DISK_RESERVE_BYTES))
        )

    def new_queue(self) -> Downloads:
        return Downloads(config_loader=lambda: self.config, clock=lambda: self.now)

    def enqueued_ids(self) -> list[str]:
        return [book.id for book in self.queue.queued_books(self.config)]

    def status(self, item_id: str) -> str:
        return self.queue.status(self.config, item_id)[0]

    def test_wifi_drop_keeps_partial_and_retries_automatically_while_other_book_can_finish(self):
        first, second = self.books
        self.client.open_download.side_effect = [
            AudioResponse(b"12", disconnected=True), AudioResponse(b"abcdef"),
            AudioResponse(b"3456", resumed=True),
        ]
        self.queue.start(self.config, first)
        self.queue.start(self.config, second)
        self.queue.start(self.config, first)
        self.assertEqual(self.enqueued_ids(), ["a", "b"])

        self.assertTrue(self.queue.process_next())
        self.assertEqual(self.queue.status(self.config, "a"), (WAITING, 2, 6))
        self.assertFalse(read_download(book_directory(self.config, "a")).complete)
        self.assertTrue(self.queue.process_next())
        self.assertEqual(self.status("b"), DOWNLOADED)
        self.assertFalse(self.queue.process_next())

        self.now += 10
        self.assertTrue(self.queue.process_next())
        self.assertEqual(self.client.open_download.call_args.args, ("/api/items/a/file/123/download", 2))
        self.assertEqual(self.client.open_download.call_args.kwargs["validator"], '"revision"')
        self.assertEqual((book_directory(self.config, "a") / "track-1").read_bytes(), b"123456")
        self.assertEqual(self.status("a"), DOWNLOADED)
        self.assertEqual(self.enqueued_ids(), [])

    def test_reboot_during_active_transfer_recovers_partial_and_remaining_queue(self):
        first, second = self.books
        before_reboot = self.queue

        class Reboot(BaseException):
            pass

        class InterruptedResponse(AudioResponse):
            def read(self, size=-1):
                if self.tell():
                    raise Reboot
                return super().read(size)

        before_reboot.start(self.config, first)
        before_reboot.start(self.config, second)
        self.client.open_download.return_value = InterruptedResponse(b"12")
        with self.assertRaises(Reboot):
            before_reboot.process_next()
        directory = book_directory(self.config, "a")
        self.assertEqual((directory / "track-1.part").read_bytes(), b"12")
        queue_file = account_directory(self.config) / "queue.json"
        queue_text = queue_file.read_text()
        self.assertNotIn("secret", queue_text)
        self.assertNotIn("token=private", queue_text)

        self.config = replace(self.config, token="updated-token")
        self.queue = self.new_queue()
        self.assertEqual(self.enqueued_ids(), ["a", "b"])
        self.assertEqual(self.queue.queued_books(self.config)[0].series_memberships, first.series_memberships)
        self.client.open_download.side_effect = [AudioResponse(b"3456", resumed=True), AudioResponse(b"abcdef")]
        self.assertTrue(self.queue.process_next())
        self.client_factory.assert_called_with(self.config)
        self.assertEqual(self.client.open_download.call_args.args, ("/api/items/a/file/123/download", 2))
        self.assertTrue(self.queue.process_next())
        self.assertEqual(self.client.open_download.call_args.args, ("/api/items/b/file/123/download", 0))
        self.assertEqual([self.status(book.id) for book in self.books], [DOWNLOADED, DOWNLOADED])

    def test_reboot_offline_keeps_waiting_book_until_connection_returns(self):
        self.queue.start(self.config, self.books[0])
        def request_offline(*args, **kwargs):
            raise AudiobookshelfError("Audiobookshelf is offline") from URLError("offline")

        self.client.request.side_effect = request_offline
        self.assertTrue(self.queue.process_next())
        self.queue = self.new_queue()
        self.assertEqual(self.status("a"), WAITING)
        self.assertTrue(self.queue.process_next())
        self.assertFalse(self.queue.process_next())

        self.client.request.side_effect = None
        self.client.open_download.return_value = AudioResponse(b"123456")
        self.now += 10
        self.assertTrue(self.queue.process_next())
        self.assertEqual(self.status("a"), DOWNLOADED)

    def test_wrong_range_is_never_appended_to_a_partial_download(self):
        wrong_range = AudioResponse(b"3456", resumed=True)
        wrong_range.headers["Content-Range"] = "bytes 1-4/6"
        self.client.open_download.side_effect = [AudioResponse(b"12", disconnected=True), wrong_range]
        self.queue.start(self.config, self.books[0])
        self.assertTrue(self.queue.process_next())
        self.now += 10
        self.assertTrue(self.queue.process_next())
        self.assertIn("different byte range", self.status("a"))
        directory = book_directory(self.config, "a")
        self.assertFalse(read_download(directory).complete)
        self.assertEqual((directory / "track-1.part").read_bytes(), b"12")
        self.assertFalse(self.queue.process_next())

    def test_partial_without_a_validator_restarts_from_byte_zero(self):
        unvalidated = AudioResponse(b"12", disconnected=True)
        unvalidated.headers.clear()
        self.client.open_download.side_effect = [unvalidated, AudioResponse(b"abcdef")]
        self.queue.start(self.config, self.books[0])
        self.assertTrue(self.queue.process_next())
        self.now += 10
        self.assertTrue(self.queue.process_next())
        self.assertEqual(self.client.open_download.call_args.args, ("/api/items/a/file/123/download", 0))
        self.assertEqual((book_directory(self.config, "a") / "track-1").read_bytes(), b"abcdef")

    def test_rejected_full_response_preserves_the_partial_and_its_validator(self):
        invalid = AudioResponse(b"error")
        invalid.headers = {"Content-Length": "5", "ETag": '"different"'}
        self.client.open_download.side_effect = [
            AudioResponse(b"12", disconnected=True), invalid, AudioResponse(b"3456", resumed=True),
        ]
        book = self.books[0]
        self.queue.start(self.config, book)
        self.queue.process_next()
        self.now += 10
        self.queue.process_next()
        directory = book_directory(self.config, book.id)
        self.assertIn("audio file changed", self.status(book.id))
        self.assertEqual((directory / "track-1.part").read_bytes(), b"12")
        self.assertEqual((directory / "track-1.validator").read_text(), '"revision"')

        self.queue = self.new_queue()
        self.queue.start(self.config, book)
        self.queue.process_next()
        self.assertEqual(self.status(book.id), DOWNLOADED)
        self.assertEqual((directory / "track-1").read_bytes(), b"123456")

    def test_empty_replacement_body_preserves_a_resumable_partial(self):
        empty = AudioResponse(b"")
        empty.headers = {"Content-Length": "6", "ETag": '"new-edition"'}
        self.client.open_download.side_effect = [AudioResponse(b"12", disconnected=True), empty]
        book = self.books[0]
        self.queue.start(self.config, book)
        self.queue.process_next()
        self.now += 10
        self.queue.process_next()
        directory = book_directory(self.config, book.id)
        self.assertEqual(self.queue.status(self.config, book.id), (WAITING, 2, 6))
        self.assertEqual((directory / "track-1.validator").read_text(), '"revision"')

    def test_offline_retry_keeps_an_unvalidated_partial_until_replacement_arrives(self):
        first = AudioResponse(b"12", disconnected=True)
        first.headers.clear()
        self.client.open_download.side_effect = [first, URLError("offline"), AudioResponse(b"abcdef")]
        book = self.books[0]
        self.queue.start(self.config, book)
        self.queue.process_next()
        self.now += 10
        self.queue.process_next()
        self.assertEqual(self.queue.status(self.config, book.id), (WAITING, 2, 6))
        self.now += 10
        self.queue.process_next()
        self.assertEqual((book_directory(self.config, book.id) / "track-1").read_bytes(), b"abcdef")

    def test_cancel_queued_and_waiting_jobs_persists_after_restart(self):
        self.queue.start(self.config, self.books[0])
        self.queue.start(self.config, self.books[1])
        self.client.open_download.return_value = AudioResponse(b"12", disconnected=True)
        self.assertTrue(self.queue.process_next())
        self.queue.cancel(self.config, "a")
        self.queue.cancel(self.config, "b")

        self.queue = self.new_queue()
        self.assertTrue(self.status("a").startswith("Cancelled"))
        self.assertTrue(self.status("b").startswith("Cancelled"))
        self.assertFalse(self.queue.process_next())
        self.queue.remove(self.config, "b")
        self.assertEqual(self.enqueued_ids(), ["a"])

    def test_cancelling_active_transfer_does_not_overwrite_new_retry(self):
        queue = self.queue
        config = self.config
        first = self.books[0]

        def cancel_and_retry():
            self.assertEqual(queue.status(config, first.id)[0], DOWNLOADING)
            self.assertFalse(queue.process_next())
            queue.cancel(config, first.id)
            queue.start(config, first)

        class CancelledResponse(AudioResponse):
            def read(self, size=-1):
                if self.tell():
                    cancel_and_retry()
                return super().read(size)

        self.queue.start(self.config, first)
        self.client.open_download.side_effect = [CancelledResponse(b"12"), AudioResponse(b"3456", resumed=True)]
        self.assertTrue(self.queue.process_next())
        self.assertEqual(self.status("a"), QUEUED)
        self.assertTrue(self.queue.process_next())
        self.assertEqual(self.status("a"), DOWNLOADED)

    def test_permanent_failures_are_visible_and_require_manual_retry(self):
        self.queue.start(self.config, self.books[0])
        self.queue.start(self.config, self.books[1])
        self.client.open_download.side_effect = HTTPError("https://example.test/secret", 403, "Forbidden", {}, None)
        self.assertTrue(self.queue.process_next())
        self.disk.return_value = (0, DISK_RESERVE_BYTES)
        self.assertTrue(self.queue.process_next())
        self.queue = self.new_queue()
        self.assertIn("account permissions", self.status("a"))
        self.assertNotIn("example.test", self.status("a"))
        self.assertIn("insufficient free space", self.status("b"))
        self.now += 10
        self.assertFalse(self.queue.process_next())
        self.assertFalse(book_directory(self.config, "b").exists())
        self.client.open_download.assert_called_once()

    def test_worker_starts_from_disk_without_an_admin_request_for_current_account(self):
        self.queue.start(self.config, self.books[0])
        self.queue = self.new_queue()
        original_config = self.config
        self.config = replace(self.config, username="other")
        self.assertFalse(self.queue.process_next())
        self.assertEqual(self.enqueued_ids(), [])
        self.client.request.assert_not_called()

        self.config = original_config
        processed = threading.Event()
        process_next = self.queue.process_next

        def signal_completion():
            result = process_next()
            if result:
                processed.set()
            return result

        self.queue.process_next = signal_completion
        self.client.open_download.return_value = AudioResponse(b"123456")
        self.addCleanup(self.queue.close)
        self.queue.start_worker()
        self.assertTrue(processed.wait(timeout=3))
        self.assertEqual(self.status("a"), DOWNLOADED)

    def test_setup_server_starts_and_stops_the_background_worker(self):
        with (
            patch("abs_kids_player.setup_server.SetupServer") as server,
            patch("abs_kids_player.setup_server.log_installed_build"),
            patch("abs_kids_player.setup_server.downloads") as worker,
            patch("builtins.print"),
        ):
            server.return_value.serve_forever.side_effect = worker.start_worker.assert_called_once
            run_setup_server()
        worker.close.assert_called_once()
        server.return_value.server_close.assert_called_once()

    def test_downloads_view_shows_queue_even_when_filters_or_catalogue_exclude_books(self):
        self.queue.start(self.config, self.books[0])
        self.queue.start(self.config, self.books[1])
        self.client.open_download.return_value = AudioResponse(b"12", disconnected=True)
        self.assertTrue(self.queue.process_next())
        with self.assertRaisesRegex(DownloadError, "Cancel"):
            self.queue.remove(self.config, "a")

        with patch("abs_kids_player.download_views.downloads", self.queue):
            page = render_library(self.config, [], "unmatched", True, 1, "csrf", view="authors")
        queue_section = page.split('id="download-queue"', 1)[1].split("</section>", 1)[0]
        self.assertLess(queue_section.index("First book"), queue_section.index("Second book"))
        self.assertIn(WAITING, queue_section)
        self.assertIn(QUEUED, queue_section)
        self.assertIn('aria-label="Download progress"', queue_section)
        self.assertEqual(queue_section.count('value="cancel"'), 2)
        self.assertNotIn('value="remove"', queue_section)
        self.assertIn("No matching authors.", page)

    def test_post_retry_uses_saved_book_when_catalogue_no_longer_contains_it(self):
        self.queue.start(self.config, self.books[0])
        self.queue.cancel(self.config, "a")
        handler = object.__new__(SetupHandler)
        handler.path = "/library/download"
        handler.csrf_token = Mock(return_value="csrf")
        handler.post_fields = Mock(return_value={
            "csrf_token": ["csrf"], "book_id": ["a"], "action": ["download"], "downloaded": ["1"],
        })
        handler.redirect_library = Mock()
        handler.send_request_error = Mock()
        with (
            patch("abs_kids_player.download_routes.downloads", self.queue),
            patch("abs_kids_player.download_routes.load_config", return_value=self.config),
            patch("abs_kids_player.web_library.load_cached_books", return_value=[]),
        ):
            handler.do_POST()
        handler.send_request_error.assert_not_called()
        handler.redirect_library.assert_called_once_with(handler.post_fields.return_value)
        self.assertEqual(self.status("a"), QUEUED)

    def test_fetch_download_queues_book_and_returns_controls_without_a_redirect(self):
        handler = object.__new__(SetupHandler)
        handler.path = "/library/download"
        handler.headers = {"X-Requested-With": "fetch"}
        handler.csrf_token = Mock(return_value="csrf")
        handler.post_fields = Mock(return_value={
            "csrf_token": ["wrong"], "book_id": ["a"], "action": ["download"],
        })
        handler.send_json = Mock()
        handler.send_csrf_error = Mock()
        handler.redirect_library = Mock()
        with (
            patch("abs_kids_player.download_routes.load_config", return_value=self.config),
            patch("abs_kids_player.web_library.load_cached_books", return_value=self.books),
            patch("abs_kids_player.download_routes.downloads", self.queue),
        ):
            handler.do_POST()
            handler.send_csrf_error.assert_called_once()
            self.assertEqual(self.enqueued_ids(), [])
            handler.send_json.assert_not_called()

            handler.post_fields.return_value["csrf_token"] = ["csrf"]
            handler.do_POST()
            self.assertEqual(self.enqueued_ids(), ["a"])
            state = handler.send_json.call_args.args[0]["card"]
            self.assertEqual(state["label"], QUEUED)
            self.assertIn('value="cancel"', state["buttons"])
            handler.redirect_library.assert_not_called()

            handler.post_fields.return_value["action"] = ["remove"]
            handler.do_POST()
            self.assertEqual(handler.send_json.call_args.kwargs["status"], 409)
            self.assertIn("Cancel the download", handler.send_json.call_args.args[0]["error"])
            handler.redirect_library.assert_not_called()

    def test_status_route_returns_live_progress_and_inventory_from_disk(self):
        self.queue.start(self.config, self.books[0])
        self.client.open_download.side_effect = [AudioResponse(b"12", disconnected=True),
                                                 AudioResponse(b"3456", resumed=True)]
        self.assertTrue(self.queue.process_next())
        handler = object.__new__(SetupHandler)
        handler.path = "/library/download/status?downloaded=1&view=authors&q=First&id=a"
        handler.csrf_token = Mock(return_value="csrf")
        handler.send_json = Mock()

        with (
            patch("abs_kids_player.setup_server.load_config", return_value=self.config),
            patch("abs_kids_player.download_routes.downloads", self.queue),
            patch("abs_kids_player.download_views.downloads", self.queue),
            patch("abs_kids_player.web_catalogue.load_library_books") as load,
            patch("abs_kids_player.setup_server.shelf_groups") as shelves,
        ):
            handler.do_GET()
            pending = handler.send_json.call_args.args[0]
            self.assertEqual(pending["csrf"], "csrf")
            self.assertIn(WAITING, pending["cards"]["a"]["label"])
            self.assertIn('max="6" value="2"', pending["cards"]["a"]["progress"])
            self.assertIn('data-download-id="a"', pending["queue"])
            self.assertIn('name="q" value="First"', pending["queue"])
            self.assertIn('name="view" value="authors"', pending["queue"])
            self.assertNotIn("token=private", pending["queue"])

            self.now += 10
            self.assertTrue(self.queue.process_next())
            handler.do_GET()
            completed = handler.send_json.call_args.args[0]
            self.assertNotEqual(completed["inventory"], pending["inventory"])
            self.assertEqual(completed["queue"], "")
            self.assertEqual(completed["cards"]["a"]["label"], "")
            self.assertIn("icon-trash", completed["cards"]["a"]["buttons"])
            self.assertEqual(completed["cards"]["a"]["progress"], "")
            load.assert_not_called()
            shelves.assert_not_called()

    def web_book_request(self, book: Book, section: str, downloaded_only: bool) -> MenuCommand:
        handler = object.__new__(SetupHandler)
        handler.path = "/player"
        handler.headers = {"X-Requested-With": "fetch"}
        handler.post_fields = Mock(return_value={
            "csrf_token": ["csrf"], "action": ["play_book"], "book_id": [book.id],
            "section": [section], "downloaded": [str(int(downloaded_only))],
        })
        handler.csrf_token = Mock(return_value="csrf")
        handler.send_json = Mock()
        with (
            patch("abs_kids_player.setup_server.load_config", return_value=self.config),
            patch("abs_kids_player.setup_server.load_cached_books", return_value=self.books),
            patch("abs_kids_player.oled_service.load_config", return_value=self.config),
        ):
            handler.do_POST()
            web, = consume_web_player_commands()
            self.assertEqual(handler.send_json.call_args.kwargs["status"], 202)
            self.assertEqual(handler.send_json.call_args.args[0]["id"], web.id)
            self.assertEqual(web.catalogue_key, catalogue_request_key(self.config))
            owner = Mock(spec=PlaybackOwner, starting_book=False, state=PlaybackSnapshot())
            handle_web_book_request(web, 0, ApplianceMenu(), owner)
            owner.request_book.assert_called_once()
            return owner.request_book.call_args.args[0]

    def test_play_on_chapter_uses_the_scoped_download_and_recovers_saved_position(self):
        first = replace(self.books[0], current_time=19)
        self.books[0] = first
        self.queue.start(self.config, first)
        self.client.open_download.return_value = AudioResponse(b"123456")
        self.assertTrue(self.queue.process_next())

        for section, downloaded_only, prefer in (
            ("downloads", True, False), ("downloads", False, False), ("library", False, True),
        ):
            self.config = replace(self.config, prefer_downloaded=prefer)
            with (
                self.subTest(section=section, filtered=downloaded_only, prefer=prefer),
                patch("abs_kids_player.playback_start.load_config", return_value=self.config),
                patch("abs_kids_player.api.AudiobookshelfClient.start_playback") as streaming,
            ):
                command = self.web_book_request(first, section, downloaded_only)
                result = prepare_book_request(command)
                self.assertTrue(result.prepared.session.downloaded)
                self.assertEqual(result.prepared.session.current_time, 19)
                streaming.assert_not_called()

        self.config = replace(self.config, prefer_downloaded=False)
        self.assertFalse(self.web_book_request(first, "library", True).local_only)

    def test_downloads_page_handles_a_missing_copy_according_to_the_selected_view(self):
        book = self.books[0]
        self.assertFalse(self.web_book_request(book, "downloads", False).local_only)
        with (
            patch("abs_kids_player.playback_start.load_config", return_value=self.config),
            patch("abs_kids_player.api.AudiobookshelfClient.start_playback") as streaming,
        ):
            result = prepare_book_request(self.web_book_request(book, "downloads", True))
            self.assertIsNone(result.prepared)
            self.assertEqual(result.error_frame.top, "Book unavailable")
            streaming.assert_not_called()
