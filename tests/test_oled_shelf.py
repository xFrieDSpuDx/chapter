import os
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from abs_kids_player.config import AppConfig
from abs_kids_player.download_store import DownloadedBook, book_directory
from abs_kids_player.listening_control import update_menu_position
from abs_kids_player.models import AudioTrack, Book
from abs_kids_player.oled_shelf import OledShelf, check_shelf
from abs_kids_player.progress_sync import ProgressSync, progress_account
from abs_kids_player.rotary_ui import ApplianceMenu


class OledShelfTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"ABS_KIDS_PLAYER_STATE_DIR": directory}))
        self.config = AppConfig(server_url="https://library.invalid", username="reader", token="fixture", library_id="books")
        self.book = Book("book", "Book", "Reader", 1200, "")
        self.catalogue = [self.book]
        self.menu = ApplianceMenu(self.catalogue.copy())

    def save_download(self, book):
        directory = book_directory(self.config, book.id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "track-1").write_bytes(b"x")
        track = AudioTrack(1, 0, book.duration, "track-1", "Track", size_bytes=1)
        record = DownloadedBook(book, [track], [], complete=True)
        record.save(directory)
        return directory, record

    def test_rebuilds_for_download_progress_and_missing_track_but_skips_unchanged_shelf(self):
        directory, _ = self.save_download(self.book)
        state, shown = check_shelf(self.config, self.catalogue, self.menu.books, None)
        self.assertTrue(shown[0].downloaded)
        with patch("abs_kids_player.oled_shelf.sorted_books_for_menu") as sort:
            self.assertIsNone(check_shelf(self.config, self.catalogue, shown, state)[1])
            sort.assert_not_called()
        ProgressSync().record(progress_account(self.config), self.book.id, 45, self.book.duration)
        state, shown = check_shelf(self.config, self.catalogue, shown, state)
        self.assertEqual(shown[0].current_time, 45)
        (directory / "track-1").unlink()
        _, shown = check_shelf(self.config, self.catalogue, shown, state)
        self.assertFalse(shown[0].downloaded)

    def test_checks_new_and_removed_download_only_books_and_preserves_podcast_episodes(self):
        episode = replace(self.book, id="episode", episode_id="episode", podcast_title="Podcast")
        directory, _ = self.save_download(replace(self.book, id="local"))
        state, shown = check_shelf(self.config, self.catalogue, [self.book, episode], None)
        self.assertEqual({book.id for book in shown}, {"book", "episode", "local"})
        self.assertTrue(next(book for book in shown if book.id == "local").download_only)
        (directory / "book.json").unlink()
        _, shown = check_shelf(self.config, self.catalogue, shown, state)
        self.assertEqual({book.id for book in shown}, {"book", "episode"})

    def test_late_result_cannot_overwrite_an_account_sort_catalogue_or_menu_change(self):
        for change in ("account", "sort", "catalogue", "menu"):
            with self.subTest(change=change), OledShelfTestWorker() as worker:
                self.save_download(self.book)
                worker.shelf.request(self.config, self.catalogue, self.menu)
                self.assertTrue(worker.started.wait(2))
                worker.shelf.request(self.config, self.catalogue, self.menu)
                worker.shelf.apply_ready(self.config, self.catalogue, self.menu)
                worker.finish()
                config, catalogue = self.config, self.catalogue
                if change == "account":
                    config = replace(config, username="other")
                elif change == "sort":
                    config = replace(config, library_sort_mode="author")
                elif change == "catalogue":
                    catalogue = self.catalogue.copy()
                else:
                    self.menu.set_books([replace(self.book, title="New selection")])
                worker.shelf.apply_ready(config, catalogue, self.menu)
                self.assertFalse(self.menu.books[0].downloaded)
                worker.probe.assert_called_once()

    def test_newer_stop_position_survives_an_in_flight_download_check(self):
        _, local = self.save_download(replace(self.book, id="other"))
        session = replace(local.local_session(book_directory(self.config, "other"), "reader", 0),
                          library_item_id=self.book.id)
        with OledShelfTestWorker() as worker:
            worker.shelf.request(self.config, self.catalogue, self.menu)
            self.assertTrue(worker.started.wait(2))
            update_menu_position(self.menu, session, 45)
            worker.finish()
            worker.shelf.apply_ready(self.config, self.catalogue, self.menu)
        _, rebuilt = check_shelf(self.config, self.catalogue, self.menu.books, None)
        self.assertEqual(next(book for book in rebuilt if book.id == "book").current_time, 45)

    def test_applies_a_completed_download_without_moving_the_dial_selection(self):
        other = replace(self.book, id="other", title="Other book")
        self.catalogue.append(other)
        self.menu.set_books(self.catalogue.copy())
        self.menu.rotate_nav(1)
        self.save_download(self.book)
        with OledShelfTestWorker() as worker:
            worker.shelf.request(self.config, self.catalogue, self.menu)
            self.assertTrue(worker.started.wait(2))
            worker.finish()
            worker.shelf.apply_ready(self.config, self.catalogue, self.menu)
        self.assertTrue(self.menu.books[0].downloaded)
        self.assertEqual(self.menu.books[self.menu.book_index].id, "other")

    def test_shelf_check_retries_when_a_worker_cannot_start(self):
        with OledShelfTestWorker() as worker:
            self.save_download(self.book)
            with patch("threading.Thread.start", side_effect=RuntimeError("temporarily unavailable")):
                worker.shelf.request(self.config, self.catalogue, self.menu)
            worker.shelf.request(self.config, self.catalogue, self.menu)
            self.assertTrue(worker.started.wait(2))
            worker.finish()
            worker.shelf.apply_ready(self.config, self.catalogue, self.menu)
            worker.probe.assert_called_once()
        self.assertTrue(self.menu.books[0].downloaded)


class OledShelfTestWorker:
    """Hold the real disk worker until the test has made a foreground change."""

    def __enter__(self):
        self.shelf = OledShelf()
        self.started, self.release = threading.Event(), threading.Event()
        def blocked(*args):
            self.started.set()
            if not self.release.wait(3):
                raise TimeoutError("test did not release the disk check")
            return check_shelf(*args)
        self.patch = patch("abs_kids_player.oled_shelf.check_shelf", side_effect=blocked)
        self.probe = self.patch.start()
        return self

    def finish(self):
        self.release.set()
        self.shelf.close()

    def __exit__(self, *exc):
        self.finish()
        self.patch.stop()
