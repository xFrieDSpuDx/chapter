"""Build the browser's shelves from Chapter's catalogue and Audiobookshelf."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .api import client_from_config
from .config import LIBRARY_SORT_AUTHOR, LIBRARY_SORT_AUTHOR_FIRST, AppConfig
from .library import (
    author_sort_text,
    book_section_letter,
    books_in_series,
    series_key,
    sorted_books_for_menu,
    title_sort_text,
)
from .library_cache import load_cached_books
from .models import Book
from .setup_http import one


BROWSE_VIEWS = {
    "titles": "Titles",
    "series": "Series",
    "authors": "Authors",
    "collections": "Collections",
    "playlists": "Playlists",
    "pinned": "Pinned shelf",
    "continue": "Continue listening",
    "letters": "A–Z",
    "podcasts": "Podcasts",
}


@dataclass(frozen=True)
class BookGroup:
    key: str
    name: str
    books: list[Book]


def browser_shelves(config: AppConfig, view: str) -> list:
    data = client_from_config(config).request("GET", f"/api/{view}")
    rows = data.get(view, []) if isinstance(data, dict) else data
    return rows if isinstance(rows, list) else []


def shelf_groups(config: AppConfig, view: str, books: list[Book], *, rows: list | None = None) -> list[BookGroup]:
    if view not in ("collections", "playlists") or not config.is_ready:
        return []
    if rows is None:
        rows = browser_shelves(config, view)
    available = {book.id: book for book in books}
    groups = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id") or not row.get("name"):
            continue
        if config.library_id and row.get("libraryId") not in (None, "", config.library_id):
            continue
        members = row.get("books" if view == "collections" else "items")
        if not isinstance(members, list):
            members = []
        book_ids = {}
        for item in members:
            if isinstance(item, str):
                book_ids[item] = None
            elif isinstance(item, dict) and not item.get("episodeId"):
                book_ids[str(item.get("libraryItemId") or item.get("id") or "")] = None
        groups.append(BookGroup(str(row["id"]), str(row["name"]), [
            available[item_id] for item_id in book_ids if item_id in available
        ]))
    return sorted(groups, key=lambda group: title_sort_text(group.name))


def group_books(config: AppConfig, books: list[Book], view: str) -> list[BookGroup]:
    entries: dict[str, list[Book]] = defaultdict(list)
    labels = {}
    for book in books:
        if view == "series":
            memberships = {series_key(member): member.name for member in book.series}
        elif view == "authors":
            author = book.author.strip()
            memberships = {author.casefold() or "~unknown": author or "Unknown author"}
        elif view == "letters":
            letter = book_section_letter(book, config.library_sort_mode)
            memberships = {letter: letter}
        elif view == "podcasts" and book.podcast_title:
            memberships = {book.source_item_id: book.podcast_title}
        else:
            memberships = {}
        for key, label in memberships.items():
            entries[key].append(book)
            labels.setdefault(key, label)
    groups = []
    for key, members in entries.items():
        if view == "series":
            members = books_in_series(members, key)
        elif view == "authors":
            order = LIBRARY_SORT_AUTHOR_FIRST if config.library_sort_mode == LIBRARY_SORT_AUTHOR_FIRST else LIBRARY_SORT_AUTHOR
            members = sorted_books_for_menu(members, order)
        elif view == "letters":
            members = sorted_books_for_menu(members, config.library_sort_mode)
        groups.append(BookGroup(key, labels[key], members))
    if view == "authors":
        first_name = config.library_sort_mode == LIBRARY_SORT_AUTHOR_FIRST
        return sorted(groups, key=lambda group: author_sort_text(group.key, first_name=first_name))
    return sorted(groups, key=lambda group: title_sort_text(group.name))


def find_library_book(config: AppConfig, item_id: str) -> Book | None:
    if not config.is_ready or not item_id or item_id.startswith("podcast:"):
        return None
    return next((book for book in load_cached_books(config) if book.id == item_id and not book.episode_id), None)


def library_display_filters(fields: dict[str, list[str]]) -> dict:
    section = one(fields, "section")
    downloaded_only = one(fields, "downloaded") == "1"
    if section not in ("library", "downloads"):
        section = "downloads" if downloaded_only else "library"
    return {
        "section": section,
        "hide_downloaded": not downloaded_only and one(fields, "hide_downloaded") == "1",
        "hide_finished": one(fields, "hide_finished") == "1",
    }


def library_page(fields: dict[str, list[str]]) -> int:
    try:
        return max(1, int(one(fields, "page")))
    except ValueError:
        return 1
