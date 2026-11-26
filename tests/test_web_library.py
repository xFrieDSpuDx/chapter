from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from abs_kids_player.config import AppConfig
from abs_kids_player.download_views import library_url, render_library, render_library_row
from abs_kids_player.models import Book, SeriesMembership
from abs_kids_player.setup_server import SetupHandler
from abs_kids_player.web_library import group_books, shelf_groups


class WebLibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(server_url="https://books.example.test", token="private", library_id="lib")
        self.first = Book("one", "Zebra", "Chris Grey", 60, "https://books.example.test/cover?token=private",
                          series_memberships=(SeriesMembership("series", "A series", "1.5"),))
        self.second = Book("two", "Alpha", "Chris Grey", 60, "",
                           series_memberships=(SeriesMembership("series", "A series", "10"),
                                               SeriesMembership("other", "Another series", "1")))

    def test_series_keeps_all_memberships_and_orders_numbers(self):
        groups = {group.key: group for group in group_books(self.config, [self.second, self.first], "series")}
        self.assertEqual(groups["series"].books, [self.first, self.second])
        self.assertEqual(groups["other"].books, [self.second])

    def test_download_view_keeps_a_readable_author_group_when_the_author_is_missing(self):
        self.first.author = ""
        group, = group_books(self.config, [self.first], "authors")
        self.assertTrue(group.key)
        self.assertEqual(group.name, "Unknown author")

    def test_collection_members_are_filtered_to_the_current_library_in_shelf_order(self):
        client = Mock()
        client.request.return_value = {"collections": [
            {"id": "mine", "name": "Favourites", "libraryId": "lib",
             "books": [{"id": "two"}, "unknown", "one", "two"]},
            {"id": "other", "name": "Another library", "libraryId": "different", "books": ["one"]},
        ]}
        with patch("abs_kids_player.web_library.client_from_config", return_value=client):
            shelf, = shelf_groups(self.config, "collections", [self.first, self.second])
        self.assertEqual(shelf.books, [self.second, self.first])
        client.request.assert_called_once_with("GET", "/api/collections")

    def test_series_view_renders_local_artwork_and_keeps_group_in_actions(self):
        with (
            patch("abs_kids_player.download_views.downloads.status", return_value=("", 0, 0)),
        ):
            page = render_library(self.config, [self.second, self.first], "", False, 1, "csrf",
                                  view="series", group="series")
        self.assertIn('src="/artwork/one"', page)
        self.assertNotIn("token=private", page)
        self.assertLess(page.index("Zebra"), page.index("Alpha"))
        self.assertIn('<label for="library-view">Browse by</label>', page)
        self.assertIn('<select id="library-view" name="view">', page)
        self.assertIn('<option value="series" selected>Series</option>', page)
        self.assertIn('name="group_view" type="hidden" value="series"', page)
        self.assertIn('name="group" value="series"', page)
        self.assertIn('name="csrf_token" value="csrf"', page)
        self.assertIn("view=series&group=series", library_url("foo", True, 2, view="series", group="series"))

    def test_filter_change_clears_the_previous_group_but_search_within_it_keeps_it(self):
        handler = object.__new__(SetupHandler)
        handler.send_html = Mock()
        handler.csrf_token = Mock(return_value="csrf")
        with (
            patch("abs_kids_player.setup_server.load_config", return_value=self.config),
            patch("abs_kids_player.setup_server.load_cached_books", return_value=[self.first]),
            patch("abs_kids_player.setup_server.books_with_downloads", return_value=[self.first]),
            patch("abs_kids_player.setup_server.render_library") as render,
        ):
            for view, expected_group in (("authors", ""), ("series", "series")):
                handler.send_library({"view": [view], "group": ["series"], "group_view": ["series"], "q": ["Zebra"]})
                self.assertEqual(render.call_args.args[2], "Zebra")
                self.assertEqual(render.call_args.kwargs["view"], view)
                self.assertEqual(render.call_args.kwargs["group"], expected_group)

    def test_numbered_pages_keep_filters_and_distinguish_matches_from_the_library_total(self):
        membership = (SeriesMembership("series", "A series"),)
        books = [
            Book(str(index), f"Story {index:02}", "Chris Grey", 60, "", series_memberships=membership)
            for index in range(55)
        ]
        books[-1].is_finished = True
        with patch("abs_kids_player.download_views.downloads.status", return_value=("", 0, 0)):
            markup = render_library(self.config, books, "story", False, 2, "csrf", view="series",
                                    group="series", hide_finished=True, fragment=True)
        self.assertIn('data-total="55 books in the library"', markup)
        self.assertIn("54 books match", markup)
        self.assertIn("Showing 25–48 of 54 books", markup)
        self.assertIn('aria-current="page" aria-label="Page 2">2', markup)
        self.assertIn('page=1&amp;section=library&amp;view=series&amp;group=series&amp;hide_finished=1" aria-label="Previous page"', markup)
        self.assertIn('page=3&amp;section=library&amp;view=series&amp;group=series&amp;hide_finished=1" aria-label="Next page"', markup)
        self.assertEqual(markup.count('class="cover-tile"'), 24)

    def test_page_number_is_clamped_when_results_shrink(self):
        books = [Book(str(index), f"Story {index:02}", "", 60, "") for index in range(25)]
        with patch("abs_kids_player.download_views.downloads.status", return_value=("", 0, 0)):
            last_page = render_library(self.config, books, "", False, 3, "csrf", fragment=True)
            one_page = render_library(self.config, books[:10], "", False, 3, "csrf", fragment=True)
        self.assertIn('data-page="2"', last_page)
        self.assertIn("Showing 25 of 25 books", last_page)
        self.assertIn('aria-disabled="true" aria-label="Next page"', last_page)
        self.assertIn('data-page="1"', one_page)
        self.assertNotIn('aria-label="Results pages"', one_page)

    def test_progress_is_under_the_cover_and_actions_are_over_the_artwork(self):
        self.first.current_time = 15
        self.first.progress = .25
        for state, label in ((("Downloading", 30, 120), "Download"), (("", 0, 0), "Listening")):
            with self.subTest(label=label), patch("abs_kids_player.download_views.downloads.status", return_value=state):
                tile = render_library_row(self.config, self.first, "")
                self.assertLess(tile.index("cover-frame"), tile.index("<progress"))
                self.assertLess(tile.index("<progress"), tile.index('<div class="book-details">'))
                self.assertLess(tile.index("cover-info"), tile.index("<progress"))
                self.assertLess(tile.index('class="cover-download"'), tile.index("<progress"))
                self.assertIn(f'aria-label="{label} progress"', tile)
                self.assertIn('<div class="tile-progress-slot">', tile)
                self.assertLess(tile.index("data-play-feedback"), tile.index('<div class="tile-progress-slot">'))
