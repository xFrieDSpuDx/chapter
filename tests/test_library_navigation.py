from __future__ import annotations

import unittest
from unittest.mock import Mock

from abs_kids_player.config import LIBRARY_SORT_AUTHOR, LIBRARY_SORT_AUTHOR_FIRST
from abs_kids_player.library import books_in_series, pinned_book_positions, sorted_books_for_menu
from abs_kids_player.library_navigation import LibraryBrowser
from abs_kids_player.models import Book, PlaybackSession, SeriesMembership
from abs_kids_player.rotary_ui import ApplianceMenu, CommandType, Screen


def series_book(key: str, sequence: str, *, author: str = "Zane Adams", **changes) -> Book:
    return Book(
        key, f"Title {key}", author, 100, "",
        series_name="Collected Stories", series_sequence=sequence,
        series_memberships=(SeriesMembership("series-1", "Collected Stories", sequence),),
        **changes,
    )


def open_letter_jump(menu: ApplianceMenu) -> None:
    menu.rotate_nav(-1)
    menu.click_nav()
    menu.rotate_nav(-2)
    menu.click_nav()


class LibraryNavigationTest(unittest.TestCase):
    def test_letter_jump_lands_on_first_title_without_interrupting_playback(self) -> None:
        menu = ApplianceMenu(sorted_books_for_menu([
            Book("m2", "Moonlight", "", 100, ""),
            Book("alpha", "An Apple", "", 100, ""),
            Book("m1", "The Magician", "", 100, ""),
            Book("digits", "42 Stories", "", 100, ""),
            Book("z", "Zoo", "", 100, ""),
            Book("episode", "Hidden episode", "", 100, "", episode_id="episode"),
        ]), browsing=True)
        session = PlaybackSession("playing", "alpha", "An Apple", "", 100, 25, "", [], [])
        menu.set_session(session)
        menu.rotate_nav(-2)
        self.assertEqual(menu.click_nav()[0].type, CommandType.HOME)

        open_letter_jump(menu)
        self.assertEqual(menu.render().top, "Jump to letter")
        self.assertEqual([choice.label for choice in menu.browser.choices], ["Back", "#", "A", "M", "Z"])
        menu.rotate_nav(2)
        self.assertEqual(menu.spoken_selection().text, "M")
        self.assertEqual(menu.click_nav(), [])

        self.assertEqual(menu.screen, Screen.LIBRARY)
        self.assertEqual((menu.render().top, menu.render().section_letter), ("The Magician", "M"))
        self.assertIs(menu.session, session)
        self.assertTrue(menu.is_playing)
        self.assertEqual(menu.current_time, 25)
        menu.rotate_nav(1)
        self.assertEqual(menu.render().top, "Moonlight")

    def test_letter_jump_respects_first_and_last_name_order(self) -> None:
        books = [
            Book("first", "First name match", "Martin Young", 100, ""),
            Book("last", "Last name match", "Anna Miller", 100, ""),
            Book("other", "Other", "Zoe Adams", 100, ""),
        ]
        for mode, title in ((LIBRARY_SORT_AUTHOR_FIRST, "First name match"), (LIBRARY_SORT_AUTHOR, "Last name match")):
            with self.subTest(mode=mode):
                menu = ApplianceMenu(sorted_books_for_menu(books, mode), mode, browsing=True)
                open_letter_jump(menu)
                menu.rotate_nav(1)
                self.assertEqual(menu.render().bottom, "M")
                self.assertEqual(menu.click_nav(), [])
                self.assertEqual((menu.render().top, menu.render().section_letter), (title, "M"))

    def test_letter_jump_keeps_only_downloaded_titles_when_offline(self) -> None:
        menu = ApplianceMenu([
            Book("remote", "Apple", "", 100, ""),
            Book("local", "The Magician", "", 100, "", current_time=12, downloaded=True),
            Book("other", "Zoo", "", 100, "", downloaded=True),
        ], browsing=True)
        menu.set_offline(True)
        open_letter_jump(menu)
        self.assertEqual([choice.label for choice in menu.browser.choices], ["Back", "M", "Z"])
        self.assertEqual(menu.click_nav(), [])
        self.assertEqual(menu.render().top, "The Magician")
        menu.click_nav()
        command, = menu.click_nav()
        self.assertEqual((command.type, command.book_id, command.resume_time), (CommandType.PLAY_BOOK, "local", 12))
        self.assertTrue(command.local_only)

    def test_letter_jump_handles_a_refresh_while_a_letter_is_selected(self) -> None:
        alpha = Book("a", "Apple", "", 100, "")
        first = Book("m1", "Magician", "", 100, "")
        second = Book("m2", "Moonlight", "", 100, "")
        menu = ApplianceMenu([alpha, first, second], browsing=True)
        open_letter_jump(menu)
        menu.rotate_nav(1)
        menu.set_books([alpha, second])
        self.assertEqual(menu.render().bottom, "M")
        self.assertEqual(menu.browser.selected.book, second)
        menu.set_books([alpha])
        self.assertEqual(menu.render().bottom, "Back")
        self.assertEqual(menu.click_nav(), [])
        self.assertEqual(menu.render().top, "Browse library")

    def test_author_modes_order_groups_by_numeric_series_position(self) -> None:
        books = [series_book(key, number) for key, number in (
            ("a", "10"), ("b", "2"), ("c", "1.5"), ("d", "1"), ("e", ""),
        )]
        standalone = Book("f", "Standalone", "Alice Brown", 100, "")
        self.assertEqual(
            [book.id for book in sorted_books_for_menu([standalone, *books], LIBRARY_SORT_AUTHOR)],
            ["d", "c", "b", "a", "e", "f"],
        )
        self.assertEqual(
            [book.id for book in sorted_books_for_menu([standalone, *books], LIBRARY_SORT_AUTHOR_FIRST)],
            ["f", "d", "c", "b", "a", "e"],
        )

    def test_secondary_membership_has_its_own_order(self) -> None:
        first = series_book("a", "1")
        second = series_book("b", "2")
        first.series_memberships += (SeriesMembership("series-2", "Other Stories", "10"),)
        second.series_memberships += (SeriesMembership("series-2", "Other Stories", "2"),)
        self.assertEqual(books_in_series([first, second], "series-2"), [second, first])

    def test_knob_opens_series_and_can_continue_the_unfinished_book(self) -> None:
        menu = ApplianceMenu([
            series_book("first", "1", is_finished=True),
            series_book("second", "2", current_time=8),
        ], browsing=True)
        menu.rotate_nav(-1)
        self.assertEqual(menu.render().top, "Browse library")
        menu.click_nav()
        menu.rotate_nav(1)
        self.assertEqual(menu.render().bottom, "Series")
        menu.click_nav()
        self.assertEqual(menu.render().bottom, "Collected Stories")
        menu.click_nav()
        self.assertEqual(menu.render().bottom, "Continue series")
        command, = menu.click_nav()
        self.assertEqual((command.type, command.book_id, command.resume_time),
                         (CommandType.PLAY_BOOK, "second", 8))
        self.assertFalse(command.start_over)

    def test_continue_and_pinned_shelves_have_independent_order(self) -> None:
        books = [
            series_book("done", "1", current_time=80, is_finished=True, pinned_position=1),
            series_book("old", "2", current_time=8, progress_updated_at=4, pinned_position=0),
            series_book("recent", "3", current_time=2, progress_updated_at=8),
        ]
        browser = LibraryBrowser()
        browser.open(books)
        browser.choose()
        self.assertEqual([row.key for row in browser.choices], ["back", "recent", "old"])
        browser.move(-1)
        browser.choose()
        browser.move(2)
        browser.choose()
        self.assertEqual([row.key for row in browser.choices], ["back", "old", "done"])

    def test_pinned_collection_and_playlist_use_the_servers_order(self) -> None:
        client = Mock()
        client.request.side_effect = [
            {"books": [{"id": "b"}, "a"]},
            {"items": [{"libraryItemId": "d"}, {"libraryItemId": "audio", "episodeId": "ep"}]},
        ]
        self.assertEqual(pinned_book_positions(client, "collections/c"), {"b": 0, "a": 1})
        self.assertEqual(pinned_book_positions(client, "playlists/p"), {"d": 0})
        self.assertEqual([call.args[1] for call in client.request.call_args_list],
                         ["/api/collections/c", "/api/playlists/p"])

    def test_play_next_uses_the_series_the_listener_opened(self) -> None:
        first = series_book("a", "1")
        second = series_book("b", "2")
        first.series_memberships += (SeriesMembership("secondary", "Other Stories", "1"),)
        alternative = Book("c", "Other", "", 100, "", series_memberships=(
            SeriesMembership("secondary", "Other Stories", "2"),
        ))
        menu = ApplianceMenu([first, second, alternative], browsing=True)
        menu.selected_series = "secondary"
        menu.set_session(PlaybackSession("playing", "a", "Title a", "", 100, 70, "", [], []))
        menu.rotate_nav(2)
        menu.click_nav()
        menu.rotate_nav(-1)
        command, = menu.click_nav()
        self.assertEqual((command.book_id, command.start_over), ("c", False))
