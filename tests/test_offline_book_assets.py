"""Test offline artwork, book details and responsive download operations."""

import io
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from abs_kids_player.config import AppConfig
from abs_kids_player.download_assets import (
    DownloadAssetBackfill,
    read_details,
    read_saved_cover,
    save_cover,
)
from abs_kids_player.download_space import available_for_downloads
from abs_kids_player.download_store import book_directory, read_download
from abs_kids_player.download_transfer import DISK_RESERVE_BYTES, MIB, transfer_book
from abs_kids_player.download_views import download_space_label, render_library
from abs_kids_player.downloads import Downloads
from abs_kids_player.models import Book
from abs_kids_player.setup_server import SetupHandler
from abs_kids_player.web_artwork import fetch_artwork
from abs_kids_player.web_reads import WebReads


class OfflineBookAssetsTest(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {"ABS_KIDS_PLAYER_STATE_PATH": f"{folder}/state.json"}))
        self.config = AppConfig(
            server_url="https://offline-bundle.example", username="reader", token="token", library_id="main",
        )
        self.book = Book("book", "A great book", "A writer", 50, "")
        self.directory = book_directory(self.config, self.book.id)
        self.metadata = {"description": "A quiet evening.", "publisher": "Example"}
        self.client = Mock()
        self.client.request.return_value = {
            "media": {
                "tracks": [{"ino": "123", "duration": 50, "metadata": {"size": 4}}],
                "metadata": self.metadata,
            },
        }
        audio = io.BytesIO(b"abcd")
        audio.status = 200
        audio.headers = {}
        self.client.open_download.return_value = audio

    def download(self, *, cover: tuple[str, bytes] | None):
        with (
            patch("abs_kids_player.download_transfer.client_from_config", return_value=self.client),
            patch("abs_kids_player.download_transfer.storage_usage", return_value=(0, 2 * DISK_RESERVE_BYTES)),
            patch("abs_kids_player.download_assets.request_cover", return_value=cover),
        ):
            transfer_book(self.config, self.book, self.directory, threading.Event())

    def test_complete_download_serves_both_cover_and_details_without_a_network_request(self):
        self.download(cover=("image/webp", b"saved cover"))
        self.assertTrue(read_download(self.directory).complete)
        self.assertEqual(read_details(self.directory), self.metadata)

        handler = object.__new__(SetupHandler)
        handler.path = "/library/details?id=book"
        handler.headers = {"X-Chapter-Fragment": "details"}
        handler.send_html = Mock()
        with (
            patch("abs_kids_player.setup_server.load_config", return_value=self.config),
            patch("abs_kids_player.setup_server.book_metadata") as remote,
            patch("abs_kids_player.web_artwork.client_from_config") as cover_client,
        ):
            handler.do_GET()
            self.assertEqual(fetch_artwork(self.config, "book", full=True), ("image/webp", b"saved cover"))
            cover_client.assert_not_called()
            remote.assert_not_called()
        html = handler.send_html.call_args.args[1]
        self.assertIn("A quiet evening.", html)
        self.assertIn("Synopsis", html)
        self.assertIn("/artwork/book?size=detail", html)

    def test_preview_and_detail_artwork_can_use_different_local_sizes(self):
        self.directory.mkdir(parents=True)
        with patch("abs_kids_player.download_assets.cover_thumbnail", return_value=b"small JPEG"):
            save_cover(self.directory, ("image/webp", b"full cover"))
        self.assertEqual(read_saved_cover(self.directory, full=False), ("image/jpeg", b"small JPEG"))
        self.assertEqual(read_saved_cover(self.directory, full=True), ("image/webp", b"full cover"))

    def test_old_download_is_backfilled_without_replacing_audio(self):
        self.download(cover=None)
        (self.directory / "details.json").unlink()
        original = (self.directory / "track-1").stat()
        with (
            patch("abs_kids_player.download_assets.client_from_config", return_value=self.client),
            patch("abs_kids_player.download_assets.request_cover", return_value=("image/jpeg", b"cover")),
        ):
            DownloadAssetBackfill().step(self.config)
        self.assertEqual(read_details(self.directory), self.metadata)
        self.assertEqual(read_saved_cover(self.directory), ("image/jpeg", b"cover"))
        self.assertEqual((self.directory / "track-1").stat().st_ino, original.st_ino)

    def test_space_for_downloads_uses_device_reserve_and_configured_limit(self):
        self.assertEqual(available_for_downloads(self.config, 0, DISK_RESERVE_BYTES), 0)
        self.assertEqual(available_for_downloads(self.config, 0, DISK_RESERVE_BYTES + MIB), MIB)
        size = self.config.download_limit_mib * MIB
        self.assertEqual(available_for_downloads(self.config, size - MIB, 2 * DISK_RESERVE_BYTES), MIB)
        page = render_library(self.config, [], "", True, 1, "token", space_remaining=1024 * MIB)
        self.assertIn("1,024 MiB available for downloads", page)
        self.assertEqual(download_space_label(0), "0 MiB available for downloads")

    def test_library_status_does_not_wait_for_a_queue_write(self):
        queue = Downloads()
        write_entered = threading.Event()
        release_write = threading.Event()
        status_read = threading.Event()

        def wait_for_disk(*args):
            write_entered.set()
            release_write.wait(timeout=3)

        with patch("abs_kids_player.downloads.save_download_queue", side_effect=wait_for_disk):
            writer = threading.Thread(target=queue.start, args=(self.config, self.book))
            writer.start()
            try:
                self.assertTrue(write_entered.wait(timeout=2))
                reader = threading.Thread(target=lambda: (queue.status(self.config, self.book.id), status_read.set()))
                reader.start()
                self.assertTrue(status_read.wait(timeout=1))
            finally:
                release_write.set()
                writer.join(timeout=3)
        self.assertEqual(queue.status(self.config, self.book.id)[0], "Queued")

    def test_shelf_page_returns_while_audiobookshelf_is_still_loading(self):
        started = threading.Event()
        release = threading.Event()

        def slow_lookup(*args):
            started.set()
            release.wait(timeout=3)
            return []

        handler = object.__new__(SetupHandler)
        handler.send_html = Mock()
        with (
            patch("abs_kids_player.setup_server.load_config", return_value=self.config),
            patch("abs_kids_player.setup_server.load_cached_books", return_value=[self.book]),
            patch("abs_kids_player.setup_server.web_reads", WebReads()),
            patch("abs_kids_player.setup_server.browser_shelves", side_effect=slow_lookup),
        ):
            try:
                handler.send_library({"view": ["collections"]})
                self.assertTrue(started.wait(timeout=1))
                self.assertIn("Loading shelves", handler.send_html.call_args.args[1])
            finally:
                release.set()
