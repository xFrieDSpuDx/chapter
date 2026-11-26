"""Verify space and counts stay current without rebuilding the downloads page."""

import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from abs_kids_player.config import AppConfig
from abs_kids_player.download_file_usage import directory_files, download_file_usage
from abs_kids_player.download_space import SPACE_REFRESH_SECONDS, DownloadSpace, SpaceReading
from abs_kids_player.download_store import book_directory
from abs_kids_player.download_transfer import DISK_RESERVE_BYTES, MIB
from abs_kids_player.downloads import Downloads
from abs_kids_player.models import Book
from abs_kids_player.setup_server import SetupHandler
from abs_kids_player.setup_views import format_download_summary


class DownloadLiveStatusTest(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {"ABS_KIDS_PLAYER_STATE_PATH": f"{folder}/player-state.json"}))
        self.config = AppConfig(server_url="https://books.example", username="reader", token="token", library_id="main",
                                download_limit_mib=256)
        self.book = Book("story", "A story", "A writer", 60, "")
        self.queue = Downloads(config_loader=lambda: self.config)
        self.enterContext(patch("abs_kids_player.setup_server.load_config", return_value=self.config))
        self.enterContext(patch("abs_kids_player.setup_server.downloads", self.queue))
        self.enterContext(patch("abs_kids_player.download_routes.load_config", return_value=self.config))
        self.enterContext(patch("abs_kids_player.download_routes.downloads", self.queue))
        self.enterContext(patch("abs_kids_player.download_views.downloads", self.queue))

    def test_completed_track_sizes_are_cached_but_current_partial_size_is_live(self):
        folder = book_directory(self.config, self.book.id)
        folder.mkdir(parents=True)
        (folder / "track-1").write_bytes(b"ready")
        partial = folder / "track-2.part"
        partial.write_bytes(b"p")
        self.assertEqual(download_file_usage(folder).audio_bytes, 6)
        before = directory_files.cache_info().hits
        with partial.open("ab") as file:
            file.write(b"art")
        self.assertEqual(download_file_usage(folder).audio_bytes, 9)
        self.assertEqual(directory_files.cache_info().hits, before + 1)
        partial.rename(folder / "track-2")
        self.assertEqual(download_file_usage(folder).audio_bytes, 9)
        (folder / "track-1").unlink()
        self.assertEqual(download_file_usage(folder).audio_bytes, 4)

    def test_download_space_requests_fresh_disk_usage_after_remove(self):
        sampled = threading.Event()
        reading_changed = threading.Event()
        first_started = threading.Event()
        finish_first = threading.Event()
        used = 20 * MIB
        first = True
        clock = 100.0

        def usage():
            nonlocal first
            current_used = used
            if first:
                first = False
                first_started.set()
                finish_first.wait(timeout=2)
            else:
                sampled.set()
            return current_used, 5 * DISK_RESERVE_BYTES

        def sample_finished():
            with space._lock:
                if not space._working:
                    reading_changed.set()

        space = DownloadSpace(sample=usage, clock=lambda: clock)
        refresh = space._refresh

        def refresh_with_event(revision):
            refresh(revision)
            sample_finished()

        space._refresh = refresh_with_event
        try:
            self.assertTrue(space.reading(self.config).updating)
            self.assertTrue(first_started.wait(timeout=1))
            used = 6 * MIB
            space.invalidate()
            self.assertTrue(space.reading(self.config).updating)
        finally:
            finish_first.set()
        self.assertTrue(sampled.wait(timeout=1))
        self.assertTrue(reading_changed.wait(timeout=1))
        self.assertEqual(space.reading(self.config).remaining, 250 * MIB)

        used = 10 * MIB
        clock += SPACE_REFRESH_SECONDS
        reading_changed.clear()
        space.reading(self.config)
        self.assertTrue(reading_changed.wait(timeout=1))
        self.assertEqual(space.reading(self.config).remaining, 246 * MIB)

    def test_successful_remove_invalidates_space_and_failed_remove_keeps_reading(self):
        handler = object.__new__(SetupHandler)
        handler.path = "/library/download"
        handler.headers = {"X-Requested-With": "fetch"}
        handler.post_fields = Mock(return_value={"book_id": [self.book.id], "action": ["remove"]})
        handler.send_json = Mock()
        with patch("abs_kids_player.setup_server.download_space") as space:
            handler.handle_library_download()
            space.invalidate.assert_called_once()
            space.invalidate.reset_mock()
            self.queue.start(self.config, self.book)
            handler.handle_library_download()
            space.invalidate.assert_not_called()

    def test_unchanged_queue_sends_progress_only_and_overview_reports_downloading(self):
        self.queue.start(self.config, self.book)
        handler = object.__new__(SetupHandler)
        handler.send_json = Mock()
        handler.send_download_status({"section": ["downloads"]})
        result = handler.send_json.call_args.args[0]
        self.assertEqual(result["queueHeading"], "Download queue (1)")
        self.assertIn(f'class="download-queue-row" data-download-id="{self.book.id}"', result["queue"])
        self.assertIn(self.book.title, result["queue"])
        self.assertIn("data-download-progress", result["queue"])
        self.assertIn('value="cancel"', result["queue"])
        self.assertTrue(result["queueRevision"])
        handler.send_download_status({"section": ["downloads"], "queue_revision": [result["queueRevision"]]})
        status = handler.send_json.call_args.args[0]
        self.assertNotIn("queue", status)
        self.assertEqual(status["cards"][self.book.id]["label"], "Queued")

        helper_entered = threading.Event()
        release = threading.Event()

        def transfer(*_args):
            helper_entered.set()
            release.wait(timeout=2)

        with patch("abs_kids_player.downloads.transfer_book", side_effect=transfer):
            worker = threading.Thread(target=self.queue.process_next)
            worker.start()
            try:
                self.assertTrue(helper_entered.wait(timeout=1))
                handler.path = "/status/downloads"
                with patch("abs_kids_player.setup_server.download_space") as space:
                    space.reading.return_value = SpaceReading(142 * MIB, False)
                    handler.do_GET()
                self.assertEqual(handler.send_json.call_args.args[0], {
                    "label": "0 books downloaded · 1 downloading",
                    "downloaded": "0 books downloaded",
                    "downloading": "1 downloading",
                    "queued": "1 in queue",
                    "space": "142 MiB available for downloads",
                })
                handler.send_download_status({"section": ["downloads"], "queue_revision": [result["queueRevision"]]})
                live = handler.send_json.call_args.args[0]
                self.assertNotIn("queue", live)
                self.assertEqual(live["cards"][self.book.id]["label"], "Downloading")
            finally:
                release.set()
                worker.join(timeout=2)
        self.assertEqual(format_download_summary(2, 0), "2 books downloaded")
        self.assertEqual(self.queue.activity(self.config, set()).in_queue, 0)

    def test_read_only_queue_has_progress_without_management_fields(self):
        self.queue.start(self.config, self.book)
        handler = object.__new__(SetupHandler)
        handler.send_json = Mock()
        handler.send_download_status({"section": ["downloads"], "id": [self.book.id]}, can_manage=False)
        result = handler.send_json.call_args.args[0]
        self.assertNotIn("csrf", result)
        self.assertNotIn("player", result)
        self.assertIn("data-download-progress", result["queue"])
        self.assertNotIn("<form", result["queue"])
        self.assertEqual(result["cards"][self.book.id]["buttons"], "")
