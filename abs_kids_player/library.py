"""Load books and determine their order and sections in the library menu."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from .api import AudiobookshelfClient, AudiobookshelfError, client_from_config
from .config import (
    LIBRARY_SORT_AUTHOR,
    LIBRARY_SORT_AUTHOR_FIRST,
    LIBRARY_SORT_TITLE,
    AppConfig,
    load_config,
)
from .models import Book, SeriesMembership
from .podcast import podcast_books
from .progress_sync import progress_account, read_pending_progress


IGNORED_TITLE_PREFIX_WORDS = {"a", "the"}


@dataclass(frozen=True)
class ShelfOption:
    key: str
    label: str


def available_shelves(config: AppConfig) -> list[ShelfOption]:
    client = client_from_config(config)
    shelves = []
    for kind, label in (("collections", "Collection"), ("playlists", "Playlist")):
        response = client.request("GET", f"/api/{kind}")
        rows = response.get(kind, []) if isinstance(response, dict) else response
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("id") or not row.get("name"):
                continue
            if config.library_id and row.get("libraryId") not in (None, "", config.library_id):
                continue
            shelves.append(ShelfOption(f"{kind}/{row['id']}", f"{label} · {row['name']}"))
    return sorted(shelves, key=lambda shelf: shelf.label.casefold())


def pinned_book_positions(client: AudiobookshelfClient, shelf: str) -> dict[str, int]:
    kind, _, item_id = shelf.partition("/")
    if kind not in ("collections", "playlists") or not item_id:
        return {}
    try:
        response = client.request("GET", f"/api/{kind}/{quote(item_id, safe='')}")
    except AudiobookshelfError:
        return {}
    rows = response.get("books" if kind == "collections" else "items", [])
    positions: dict[str, int] = {}
    for index, row in enumerate(rows):
        if isinstance(row, str):
            positions.setdefault(row, index)
        elif isinstance(row, dict) and not row.get("episodeId"):
            book_id = row.get("libraryItemId") or row.get("id")
            if book_id:
                positions.setdefault(str(book_id), index)
    return positions


def load_library_books(
    config: AppConfig | None = None,
    client_factory=AudiobookshelfClient,
) -> list[Book]:
    config = config or load_config()
    if not config.is_ready:
        return []

    client = client_from_config(config, client_factory)
    library_id = client.choose_library_id(config.library_id)
    books = client.get_books(library_id)
    pending = read_pending_progress()
    account = progress_account(config)
    for book in books:
        update = pending.get((account, book.id, ""))
        if update and update.updated_at >= book.progress_updated_at:
            book.current_time = update.current_time
            book.progress = max(0.0, min(1.0, update.current_time / update.duration)) if update.duration else 0
            book.progress_updated_at = update.updated_at
            book.is_finished = book.progress >= 0.995
    if config.pinned_shelf:
        positions = pinned_book_positions(client, config.pinned_shelf)
        for book in books:
            book.pinned_position = positions.get(book.id)
    return sorted_books_for_menu(
        [*books, *podcast_books(config.podcasts)],
        config.library_sort_mode,
    )


def sorted_books_for_menu(
    books: list[Book],
    sort_mode: str = LIBRARY_SORT_TITLE,
) -> list[Book]:
    return sorted(books, key=lambda book: book_menu_sort_key(book, sort_mode))


def book_menu_sort_key(
    book: Book,
    sort_mode: str = LIBRARY_SORT_TITLE,
) -> tuple:
    title = book.display_title
    if sort_mode in (LIBRARY_SORT_AUTHOR, LIBRARY_SORT_AUTHOR_FIRST):
        primary = next((member for member in book.series if member.name == book.series_name), None)
        return (
            author_sort_text(book.author, first_name=sort_mode == LIBRARY_SORT_AUTHOR_FIRST) or "\uffff",
            book.author.casefold(),
            title_sort_text(book.series_name or title),
            not bool(book.series_name),
            series_key(primary) if primary else "",
            sequence_order(book.series_sequence),
            title_sort_text(book.title),
            book.id,
        )
    return (title_sort_text(title), title.casefold(), book.author.casefold(), "")


def title_section_letter(title: str) -> str:
    words = _sortable_title_words(title)
    return _section_letter_for_word(words[0]) if words else "#"


def book_section_letter(book: Book, sort_mode: str) -> str:
    if sort_mode in (LIBRARY_SORT_AUTHOR, LIBRARY_SORT_AUTHOR_FIRST):
        return author_section_letter(book.author, first_name=sort_mode == LIBRARY_SORT_AUTHOR_FIRST)
    return title_section_letter(book.display_title)


def title_sort_text(title: str) -> str:
    return " ".join(_sortable_title_words(title)).casefold()


def _sortable_title_words(title: str) -> list[str]:
    words = _title_words(title)
    for index, word in enumerate(words):
        if word.lower() not in IGNORED_TITLE_PREFIX_WORDS:
            return words[index:]
    return words


def author_sort_text(author: str, *, first_name: bool = False) -> str:
    clean_author = " ".join(author.split())
    if not clean_author:
        return ""
    primary_author = re.split(
        r"\s*(?:,|;|&|\band\b)\s*", clean_author, maxsplit=1, flags=re.IGNORECASE
    )[0]
    if first_name:
        reversed_name = re.fullmatch(r"([^,;&]+),\s*([^,;&]+)", clean_author)
        if reversed_name:
            family, given = reversed_name.groups()
            return f"{given} {family}".casefold()
        return primary_author.casefold()
    name_parts = primary_author.split()
    if not name_parts:
        return ""
    last_name = name_parts[-1].strip(".,")
    given_names = " ".join(name_parts[:-1])
    return f"{last_name} {given_names}".strip().casefold()


def author_section_letter(author: str, *, first_name: bool = False) -> str:
    sort_text = author_sort_text(author, first_name=first_name)
    return _section_letter_for_word(sort_text) if sort_text else "#"


def sequence_order(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation:
        return Decimal("Infinity")
    return number if number.is_finite() else Decimal("Infinity")


def series_key(member: SeriesMembership) -> str:
    return member.id or member.name.casefold()


def books_in_series(books: list[Book], key: str) -> list[Book]:
    entries = []
    for book in books:
        member = next((member for member in book.series if series_key(member) == key), None)
        if member is not None:
            entries.append((sequence_order(member.sequence), title_sort_text(book.title), book.id, book))
    return [book for _, _, _, book in sorted(entries, key=lambda entry: entry[:3])]


def continue_series_book(books: list[Book]) -> Book | None:
    in_progress = [book for book in books if book.current_time > 0 and not book.finished]
    if in_progress:
        return max(in_progress, key=lambda book: book.progress_updated_at)
    return next((book for book in books if not book.finished), None)


def next_series_book(books: list[Book], active_book_id: str, selected_series: str = "") -> Book | None:
    current = next((book for book in books if book.id == active_book_id), None)
    if current is None or not current.series:
        return None
    primary = next((member for member in current.series if member.name == current.series_name), current.series[0])
    memberships = {series_key(member) for member in current.series}
    ordered = books_in_series(books, selected_series if selected_series in memberships else series_key(primary))
    index = next(index for index, book in enumerate(ordered) if book.id == active_book_id)
    return ordered[index + 1] if index + 1 < len(ordered) else None


def _title_words(title: str) -> list[str]:
    words: list[str] = []
    current: list[str] = []
    for character in title.strip():
        if character.isalnum():
            current.append(character)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return words


def _section_letter_for_word(word: str) -> str:
    first = word[0]
    if first.isdigit():
        return "#"
    return first.upper()
