from __future__ import annotations

import unittest
from unittest.mock import Mock

from abs_kids_player.config import LIBRARY_SORT_AUTHOR, AppConfig, PodcastConfig
from abs_kids_player.library import (
    author_section_letter,
    author_sort_text,
    load_library_books,
    sorted_books_for_menu,
    title_section_letter,
    title_sort_text,
)
from abs_kids_player.models import Book
from abs_kids_player.podcast import podcast_book


class LibraryTest(unittest.TestCase):
    def test_load_library_books_adds_configured_podcast_menu_item(self) -> None:
        podcast = PodcastConfig(
            url="https://podcasts.apple.com/us/podcast/story-show/id123",
            title="Story Show",
            author="Story Parent",
            feed_url="https://feeds.example.com/story.xml",
            book_id="podcast:apple:123",
        )
        client = Mock()
        client.choose_library_id.return_value = "library-1"
        client.get_books.return_value = [
            Book(id="book-1", title="The Hobbit", author="J.R.R. Tolkien", duration=1, cover_url="")
        ]
        books = load_library_books(
            AppConfig(
                server_url="https://books.example.com",
                token="token",
                library_id="library-1",
                podcasts=[podcast],
            ),
            client_factory=Mock(return_value=client),
        )

        client.get_books.assert_called_once_with("library-1")
        self.assertEqual([book.title for book in books], ["The Hobbit", "Story Show"])
        self.assertEqual(books[-1], podcast_book(podcast))

    def test_sorted_books_for_menu_merges_podcasts_with_audiobooks(self) -> None:
        books = sorted_books_for_menu(
            [
                Book(
                    id="book-1",
                    title="The Hobbit",
                    author="J.R.R. Tolkien",
                    duration=1,
                    cover_url="",
                ),
                Book(
                    id="podcast:yoto-daily",
                    title="Yoto Daily",
                    author="Yoto",
                    duration=0,
                    cover_url="",
                ),
                Book(
                    id="book-2",
                    title="A Bear Called Paddington",
                    author="Michael Bond",
                    duration=1,
                    cover_url="",
                ),
                Book(
                    id="podcast:trivia-for-kids",
                    title="Trivia for Kids",
                    author="KRCreative",
                    duration=0,
                    cover_url="",
                ),
            ]
        )

        self.assertEqual(
            [book.title for book in books],
            [
                "A Bear Called Paddington",
                "The Hobbit",
                "Trivia for Kids",
                "Yoto Daily",
            ],
        )

    def test_sorted_books_for_menu_uses_series_title(self) -> None:
        books = sorted_books_for_menu(
            [
                Book(
                    id="book-2",
                    title="Book Two",
                    author="Author",
                    duration=1,
                    cover_url="",
                    series_name="Zebra Tales",
                ),
                Book(id="book-1", title="Matilda", author="Roald Dahl", duration=1, cover_url=""),
            ]
        )

        self.assertEqual(
            [book.display_title for book in books], ["Matilda", "Zebra Tales: Book Two"]
        )

    def test_sorted_books_for_menu_can_order_by_author_last_name(self) -> None:
        books = sorted_books_for_menu(
            [
                Book(
                    id="book-1",
                    title="The Hobbit",
                    author="J.R.R. Tolkien",
                    duration=1,
                    cover_url="",
                ),
                Book(id="book-2", title="Matilda", author="Roald Dahl", duration=1, cover_url=""),
                Book(
                    id="book-3",
                    title="A Bear Called Paddington",
                    author="Michael Bond",
                    duration=1,
                    cover_url="",
                ),
                Book(id="book-4", title="Mystery Book", author="", duration=1, cover_url=""),
            ],
            LIBRARY_SORT_AUTHOR,
        )

        self.assertEqual(
            [book.title for book in books],
            ["A Bear Called Paddington", "Matilda", "The Hobbit", "Mystery Book"],
        )

    def test_title_section_letter_uses_first_title_letter(self) -> None:
        self.assertEqual(title_section_letter("  the hobbit"), "H")
        self.assertEqual(title_section_letter("A Wrinkle in Time"), "W")
        self.assertEqual(title_section_letter("The A Team"), "T")
        self.assertEqual(title_section_letter("Theodore Boone"), "T")
        self.assertEqual(title_section_letter("Anansi Boys"), "A")
        self.assertEqual(title_section_letter("...Matilda"), "M")
        self.assertEqual(title_section_letter("1984"), "#")

    def test_title_sort_text_ignores_leading_articles(self) -> None:
        self.assertEqual(title_sort_text("The Hobbit"), "hobbit")
        self.assertEqual(title_sort_text("A Wrinkle in Time"), "wrinkle in time")
        self.assertEqual(title_sort_text("The A Team"), "team")
        self.assertEqual(title_sort_text("Theodore Boone"), "theodore boone")
        self.assertEqual(title_sort_text("Anansi Boys"), "anansi boys")

    def test_author_sort_text_uses_first_authors_last_name(self) -> None:
        self.assertEqual(author_sort_text("J.R.R. Tolkien"), "tolkien j.r.r.")
        self.assertEqual(author_sort_text("Mary Pope Osborne"), "osborne mary pope")
        self.assertEqual(author_sort_text("Neil Gaiman & Terry Pratchett"), "gaiman neil")
        self.assertEqual(author_sort_text("Dahl, Roald"), "dahl")
        self.assertEqual(author_sort_text(""), "")
        self.assertEqual(author_section_letter("Madeleine L'Engle"), "L")
