import io
import tempfile
import threading
import unittest
from hashlib import sha256
from unittest.mock import Mock, patch

from abs_kids_player.book_details_views import render_book_details
from abs_kids_player.config import AppConfig
from abs_kids_player.connection_health import ConnectionHealth, ConnectionState
from abs_kids_player.download_views import (
    download_card_state,
    render_library,
    render_library_row,
    search_books,
)
from abs_kids_player.models import Book
from abs_kids_player.player_control import WebPlayerStatus
from abs_kids_player.setup_server import SetupHandler
from abs_kids_player.setup_views import (
    format_download_activity,
    render_settings_page,
    render_status_panel,
)
from abs_kids_player.web_catalogue import CatalogueLoader
from abs_kids_player.web_device_status import BluetoothStatusCache
from abs_kids_player.web_reads import WebReads
from abs_kids_player.wifi import WifiStatus


class LiveLibraryTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {"ABS_KIDS_PLAYER_STATE_DIR": directory}))
        self.config = AppConfig(server_url="https://books.example", token="token", username="reader", library_id="books")
        self.books = [
            Book("one", "First Story", "Ann Reader", 60, "", narrator="Narrator hidden", downloaded=True),
            Book("two", "Second Story", "Ben Reader", 60, "", is_finished=True),
            Book("three", "Third Story", "Ann Reader", 60, ""),
        ]

    def test_filters_count_eligible_books_and_keep_the_view_identity(self):
        visible = search_books(self.books, "story", False, hide_downloaded=True, hide_finished=True)
        self.assertEqual([book.id for book in visible], ["three"])
        result = render_library(
            self.config, self.books, "story", True, 1, "csrf", section="library", fragment=True,
            hide_finished=True,
        )
        self.assertTrue(result.startswith('<div class="library-results"'))
        self.assertIn('data-total="3 books in the library"', result)
        self.assertIn("1 book matches", result)
        self.assertIn("First Story", result)
        self.assertNotIn("Narrator hidden", result)
        self.assertNotIn("Third Story", result)

        page = render_library(self.config, [], "", True, 1, "csrf", section="downloads")
        self.assertIn('name="downloaded" type="checkbox" value="1" checked', page)
        self.assertIn("0 books match", page)
        self.assertIn('data-library-section="downloads"', page)
        self.assertNotIn("Refresh download status", page)

    def test_cover_controls_follow_playback_and_download_states(self):
        tile = render_library_row(
            self.config, self.books[0], "", download_status=("Downloaded", 10, 10),
            player=WebPlayerStatus(cover_item_id="one", is_playing=True),
        )
        self.assertIn("icon-info", tile)
        self.assertIn("icon-pause", tile)
        self.assertIn('name="action" value="pause_book"', tile)
        self.assertIn("icon-trash", tile)
        self.assertNotIn("Downloaded", tile)
        paused_tile = render_library_row(
            self.config, self.books[0], "", download_status=("Downloaded", 10, 10),
            player=WebPlayerStatus(cover_item_id="one", is_playing=False),
        )
        self.assertIn("icon-play", paused_tile)
        self.assertIn('name="action" value="play_book"', paused_tile)
        queued = download_card_state("Queued", 0, 0)
        self.assertIn("download-waiting", queued["progress"])
        active = download_card_state("Downloading", 10, 50)
        self.assertIn('max="50" value="10"', active["progress"])
        self.assertEqual(active["label"], "Downloading · 20%")

    def test_detail_fragment_escapes_book_metadata_and_renders_synopsis(self):
        response = render_book_details(self.books[0], {
            "description": "<p>A good story &amp; a short one.</p><script>bad()</script>",
            "publisher": "<Unescaped Company>", "genres": ["Adventure"],
        }, fragment=True)
        self.assertIn("A good story &amp; a short one.", response)
        self.assertIn("&lt;Unescaped Company&gt;", response)
        self.assertIn("Adventure", response)
        self.assertNotIn("<script>", response)
        self.assertNotIn("bad()", response)
        self.assertNotIn("<html", response)

    def test_standalone_details_return_to_the_book_browsing_section(self):
        for section, query, parent, label in (
            ("library", "", "/library", "Library"),
            ("downloads", "&amp;section=downloads", "/library?downloaded=1&amp;section=downloads", "Downloads"),
        ):
            with self.subTest(section=section):
                tile = render_library_row(self.config, self.books[0], "", section=section)
                self.assertIn(f'href="/library/details?id=one{query}"', tile)
                details = render_book_details(self.books[0], {}, fragment=False, section=section)
                self.assertIn(f'class="text-action back-link" href="{parent}">{label}</a>', details)

    def test_overview_shows_count_download_preferences_are_in_settings(self):
        panel = render_status_panel(
            WifiStatus(False), ConnectionHealth(ConnectionState.CHECKING),
            download_activity=format_download_activity(1, 0, 2, "100 MiB available for downloads"),
        )
        self.assertIn('aria-label="Download activity"', panel)
        self.assertNotIn("<a ", panel)
        self.assertIn('<span data-overview-value="downloaded">1 book downloaded', panel)
        self.assertIn('data-overview-value="downloading" hidden', panel)
        self.assertIn('data-overview-value="queued">2 in queue', panel)
        self.assertIn('data-overview-value="space">100 MiB available for downloads', panel)
        with patch("abs_kids_player.setup_views.storage_usage", return_value=(0, 1024)):
            html = render_settings_page(self.config, "downloads", "csrf")
        self.assertIn('action="/settings/downloads"', html)
        self.assertIn("Prefer downloaded books", html)

    def test_artwork_revalidates_without_resending_existing_bytes(self):
        handler = object.__new__(SetupHandler)
        image = b"cover bytes"
        handler.headers = {"If-None-Match": f'"{sha256(image).hexdigest()}"'}
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        with patch("abs_kids_player.setup_server.fetch_artwork", return_value=("image/webp", image)):
            handler.send_artwork("one")
        handler.send_response.assert_called_once_with(304)
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_cold_library_returns_before_audiobookshelf_and_deduplicates_load(self):
        started = threading.Event()
        release = threading.Event()
        stored = threading.Event()
        loader = CatalogueLoader()

        def slow_cold_load(config):
            started.set()
            release.wait(timeout=2)
            return self.books

        def store(config, books):
            stored.set()
            return True

        with (
            patch("abs_kids_player.web_catalogue.load_library_books", side_effect=slow_cold_load) as load,
            patch("abs_kids_player.web_catalogue.load_config", return_value=self.config),
            patch("abs_kids_player.web_catalogue.save_cached_books", side_effect=store),
            patch("abs_kids_player.setup_server.web_catalogue", loader),
            patch("abs_kids_player.setup_server.load_config", return_value=self.config),
        ):
            handler = object.__new__(SetupHandler)
            handler.send_html = Mock()
            try:
                handler.send_library({})
                self.assertTrue(started.wait(timeout=1))
                page = handler.send_html.call_args.args[1]
                self.assertIn('data-catalogue-pending="1"', page)
                self.assertIn("Loading library from Audiobookshelf", page)
                self.assertFalse(stored.is_set())
                self.assertEqual(loader.request(self.config), "loading")
                load.assert_called_once_with(self.config)
            finally:
                release.set()
            self.assertTrue(stored.wait(timeout=1))

    def test_dashboard_bluetooth_check_returns_while_device_probe_waits(self):
        started = threading.Event()
        release = threading.Event()
        cache = BluetoothStatusCache()

        def slow_probe():
            started.set()
            release.wait(timeout=2)
            return True, "Connected to Headphones"

        try:
            self.assertEqual(cache.current(slow_probe), (False, "Checking Bluetooth…"))
            self.assertTrue(started.wait(timeout=1))
            self.assertEqual(cache.current(slow_probe), (False, "Checking Bluetooth…"))
        finally:
            release.set()

    def test_browser_reports_a_failed_cache_write_and_retries_after_storage_recovers(self):
        loader = CatalogueLoader()
        handler = object.__new__(SetupHandler)
        handler.send_html = Mock()
        with (patch("abs_kids_player.web_catalogue.Thread") as thread,
              patch("abs_kids_player.web_catalogue.monotonic", return_value=0) as clock,
              patch("abs_kids_player.web_catalogue.load_config", return_value=self.config),
              patch("abs_kids_player.web_catalogue.load_library_books", return_value=self.books),
              patch("abs_kids_player.setup_server.web_catalogue", loader),
              patch("abs_kids_player.setup_server.load_config", return_value=self.config)):
            with patch("abs_kids_player.library_cache.write_json_atomically", side_effect=OSError("Disk full")):
                self.assertEqual(loader.request(self.config), "loading")
                options = thread.call_args.kwargs
                options["target"](*options["args"])
            handler.send_library({})
            self.assertIn("Could not save the catalogue", handler.send_html.call_args.args[1])
            clock.return_value = 31
            self.assertEqual(loader.request(self.config), "loading")
            options = thread.call_args.kwargs
            options["target"](*options["args"])
            handler.send_library({})
            self.assertIn("First Story", handler.send_html.call_args.args[1])

    def test_failed_worker_starts_release_pending_lookups_for_retry(self):
        cache = CatalogueLoader()
        with (patch("abs_kids_player.web_catalogue.Thread") as worker,
              patch("abs_kids_player.web_catalogue.monotonic", return_value=100) as clock):
            worker.return_value.start.side_effect = [RuntimeError(), None]
            self.assertEqual(cache.request(self.config), "failed")
            self.assertFalse(cache.pending(self.config))
            self.assertEqual(cache.request(self.config), "failed")
            clock.return_value = 131
            self.assertEqual(cache.request(self.config), "loading")
            self.assertTrue(cache.pending(self.config))

        reads = WebReads()
        key = ("details", "account", "book")
        load = Mock(return_value={"title": "The Hobbit"})
        with (patch("abs_kids_player.web_reads.Thread") as worker,
              patch("abs_kids_player.web_reads.monotonic", return_value=100) as clock):
            worker.return_value.start.side_effect = [RuntimeError(), None]
            self.assertEqual(reads.get(key, load), (None, False))
            self.assertFalse(reads.pending(key))
            clock.return_value = 121
            self.assertEqual(reads.get(key, load), (None, True))
            options = worker.call_args.kwargs
            options["target"](*options["args"])
            self.assertEqual(reads.get(key, load), ({"title": "The Hobbit"}, False))

    def test_device_status_retries_after_worker_launch_failure(self):
        cache = BluetoothStatusCache(ttl=15)
        with (patch("abs_kids_player.device_status.Thread") as worker,
              patch("abs_kids_player.device_status.monotonic", return_value=100) as clock):
            worker.return_value.start.side_effect = [RuntimeError(), None]
            probe = Mock(return_value=(True, "Connected to headphones"))
            self.assertEqual(cache.current(probe), (False, "Bluetooth status unavailable"))
            clock.return_value = 116
            cache.current(probe)
            options = worker.call_args.kwargs
            options["target"](*options["args"])
            self.assertEqual(cache.current(probe), (True, "Connected to headphones"))
