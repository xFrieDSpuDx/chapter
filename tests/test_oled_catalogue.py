import unittest
from unittest.mock import Mock, patch

from abs_kids_player.catalogue_settings import CATALOGUE_COMMAND, catalogue_request_key
from abs_kids_player.config import AppConfig, ConfigReadError
from abs_kids_player.models import Book
from abs_kids_player.oled_catalogue import BookLoadResult, OledCatalogue, finish_book_load
from abs_kids_player.player_control import WebPlayerCommand


class OledCatalogueTest(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig(server_url="https://books.example", token="token", library_id="books")
        self.refresh = OledCatalogue(refresh_seconds=60)
        self.receipts = self.enterContext(patch("abs_kids_player.oled_catalogue.save_command_result"))
        self.save_cache = self.enterContext(patch("abs_kids_player.oled_catalogue.save_cached_books", return_value=True))
        self.report = self.enterContext(patch("abs_kids_player.oled_catalogue.report_catalogue_result"))

    def command(self, request_id):
        return WebPlayerCommand(CATALOGUE_COMMAND, id=request_id, catalogue_key=catalogue_request_key(self.config))

    def test_failed_cache_write_keeps_online_books_and_reports_the_save_failure(self):
        book = Book("one", "The Hobbit", "J.R.R. Tolkien", 60, "")
        result = BookLoadResult(books=[book])
        request = self.command("refresh")
        self.save_cache.return_value = False
        finish_book_load(result, self.config, request)
        self.assertEqual(result.books, [book])
        self.report.assert_called_once_with(self.config, "Could not save the catalogue on the player")
        self.receipts.assert_called_once_with(request, "rejected")

    def test_failed_worker_rejects_request_and_a_retry_can_start(self):
        first, retry = self.command("first"), self.command("retry")
        with (patch("abs_kids_player.oled_catalogue.threading.Thread") as thread,
              patch("abs_kids_player.oled_catalogue.load_config", return_value=self.config)):
            thread.return_value.start.side_effect = [RuntimeError("unavailable"), None]
            self.refresh.request_latest([first], self.config, 10)
            result, _ = self.refresh.poll()
            self.assertEqual(result.error_frame.bottom, "Press to retry")
            self.receipts.assert_called_once_with(first, "rejected")
            self.assertFalse(self.refresh.in_progress)
            self.refresh.request_latest([retry], self.config, 11)
            self.assertTrue(self.refresh.in_progress)
            self.assertEqual(thread.return_value.start.call_count, 2)

    def test_same_lookup_uses_latest_request_and_survives_a_settings_read_error(self):
        first, latest = self.command("first"), self.command("latest")
        def finish(_config, queue, *, generation, library_id):
            queue.put(BookLoadResult(books=[], generation=generation))

        start = Mock(side_effect=finish)
        with (patch("abs_kids_player.oled_catalogue.start_book_load_worker", start),
              patch("abs_kids_player.oled_catalogue.load_config", side_effect=[ConfigReadError(), self.config])):
            self.refresh.request_latest([first], self.config, 10)
            self.refresh.request_latest([latest], self.config, 11)
            start.assert_called_once()
            self.receipts.assert_called_once_with(first, "superseded")
            with self.assertRaises(ConfigReadError):
                self.refresh.poll()
            self.assertTrue(self.refresh.in_progress)
            result, _ = self.refresh.poll()
            self.assertEqual(result.books, [])
            self.receipts.assert_called_with(latest, "applied")
            self.assertFalse(self.refresh.in_progress)
