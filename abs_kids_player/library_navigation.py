"""Browse views backed by the same catalogue as the main book list."""

from __future__ import annotations

from dataclasses import dataclass

from .config import LIBRARY_SORT_TITLE
from .display_frames import TwoLineFrame
from .library import (
    book_section_letter,
    books_in_series,
    continue_series_book,
    series_key,
    title_sort_text,
)
from .models import Book


JUMP_TO_LETTER = "jump-to-letter"


@dataclass(frozen=True)
class BrowseChoice:
    key: str
    label: str
    book: Book | None = None
    play_now: bool = False


class LibraryBrowser:
    def __init__(self, sort_mode: str = LIBRARY_SORT_TITLE, *, offline: bool = False) -> None:
        self.sort_mode = sort_mode
        self.offline = offline
        self.books: list[Book] = []
        self.pages: list[BrowseChoice] = []
        self.choices: list[BrowseChoice] = []
        self.index = 0

    def open(self, books: list[Book]) -> None:
        self.books = books
        self.pages = [BrowseChoice("root", "Browse library")]
        self._populate()

    def set_books(self, books: list[Book]) -> None:
        self.books = books
        if self.pages:
            self._populate(self.selected.key if len(self.choices) > 1 else "")

    def set_sort_mode(self, sort_mode: str) -> None:
        self.sort_mode = sort_mode
        if self.pages and self.pages[-1].key == JUMP_TO_LETTER:
            self._populate(self.selected.key)

    def close(self) -> None:
        self.pages.clear()
        self.choices.clear()
        self.index = 0

    @property
    def selected(self) -> BrowseChoice:
        return self.choices[self.index]

    def move(self, direction: int) -> None:
        self.index = (self.index + direction) % len(self.choices)

    def choose(self) -> BrowseChoice | None:
        selected = self.selected
        if selected.book:
            return selected
        if selected.key == "back":
            previous = self.pages.pop()
            if self.pages:
                self._populate(previous.key)
            else:
                self.close()
        else:
            self.pages.append(selected)
            self._populate()
        return None

    def render(self) -> TwoLineFrame:
        if len(self.choices) == 1:
            return TwoLineFrame(self.pages[-1].label, "No titles · Back")
        return TwoLineFrame(self.pages[-1].label, self.selected.label)

    def _populate(self, selected_key: str = "") -> None:
        self.choices = [BrowseChoice("back", "Back"), *self._page_choices(self.pages[-1].key)]
        fallback = 0 if selected_key.startswith("letter/") else 1
        self.index = next((index for index, row in enumerate(self.choices) if row.key == selected_key), fallback)
        self.index = min(self.index, len(self.choices) - 1)

    def _page_choices(self, key: str) -> list[BrowseChoice]:
        if key == "root":
            return [
                BrowseChoice("continue", "Continue Listening"),
                BrowseChoice("series", "Series"),
                BrowseChoice("pinned", "Pinned shelf"),
                BrowseChoice("podcasts", "Podcasts"),
                BrowseChoice("downloaded", "Downloaded"),
                BrowseChoice(JUMP_TO_LETTER, "Jump to letter"),
            ]
        if key == "downloaded":
            return book_choices([book for book in self.books if book.downloaded])
        catalogue = [book for book in self.books if self.offline or not book.download_only]
        if key == JUMP_TO_LETTER:
            targets: dict[str, Book] = {}
            for book in catalogue:
                if not book.episode_id:
                    targets.setdefault(book_section_letter(book, self.sort_mode), book)
            return [BrowseChoice(f"letter/{letter}", letter, book) for letter, book in targets.items()]
        if key == "series":
            series = {series_key(member): member.name for book in catalogue for member in book.series}
            return [
                BrowseChoice(f"series/{id_}", label)
                for id_, label in sorted(series.items(), key=lambda entry: title_sort_text(entry[1]))
            ]
        if key.startswith("series/"):
            books = books_in_series(catalogue, key.removeprefix("series/"))
            chosen = continue_series_book(books)
            choices = [BrowseChoice("continue-series", "Continue series", chosen, True)] if chosen else []
            return [*choices, *book_choices(books)]
        if key == "continue":
            books = [book for book in catalogue if book.current_time > 0 and not book.finished]
            return book_choices(sorted(books, key=lambda book: -book.progress_updated_at))
        if key == "pinned":
            books = [book for book in catalogue if book.pinned_position is not None]
            return book_choices(sorted(books, key=lambda book: book.pinned_position))
        if key == "podcasts":
            shows = {book.source_item_id: book.podcast_title for book in self.books if book.podcast_title}
            return [
                BrowseChoice(f"podcast/{id_}", title)
                for id_, title in sorted(shows.items(), key=lambda entry: title_sort_text(entry[1]))
            ]
        if key.startswith("podcast/"):
            return book_choices([book for book in self.books if book.source_item_id == key.removeprefix("podcast/")])
        return []


def book_choices(books: list[Book]) -> list[BrowseChoice]:
    return [BrowseChoice(book.id, book.title, book) for book in books]
