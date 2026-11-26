"""Load and render a book's extended metadata on demand."""

from html import escape
from html.parser import HTMLParser
from http.client import HTTPException
from urllib.parse import quote

from .admin_layout import render_admin_page
from .api import AudiobookshelfError, client_from_config
from .config import AppConfig
from .download_assets import select_details
from .models import Book
from .player_views import queue_book_form
from .rotary_ui import format_time
from .web_artwork import artwork_url


class Synopsis(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.ignored = False

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.ignored = False
        if tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self.ignored = True
        if tag == "br":
            self.parts.append("\n")


def book_metadata(config: AppConfig, book: Book) -> dict:
    try:
        item = client_from_config(config).request(
            "GET", f"/api/items/{quote(book.id, safe='')}", query={"expanded": 1},
        )
    except (AudiobookshelfError, HTTPException, OSError, ValueError):
        return {}
    return select_details(item)


def render_book_details(
    book: Book, metadata: dict, *, fragment: bool, pending: bool = False, guest_sections: tuple[str, ...] | None = None,
    section: str = "library", csrf: str = "",
) -> str:
    synopsis = Synopsis()
    synopsis.feed(str(metadata.get("description") or "")[:65536])
    text = "".join(synopsis.parts).strip()
    series = ", ".join(f"{series.name} ({series.sequence})" if series.sequence else series.name for series in book.series)
    genres = metadata.get("genres")
    entries = (
        ("Author", book.author), ("Series", series),
        ("Duration", format_time(book.duration) if book.duration > 0 else ""),
        ("Published", str(metadata.get("publishedYear") or "")),
        ("Publisher", str(metadata.get("publisher") or "")),
        ("Genres", ", ".join(value for value in genres if isinstance(value, str)) if isinstance(genres, list) else ""),
        ("Listening", "Finished" if book.finished else f"{format_time(book.current_time)} listened" if book.current_time else ""),
    )
    facts = "".join(
        f'<div class="status-row"><dt>{label}</dt><dd>{escape(value)}</dd></div>'
        for label, value in entries if value
    )
    summary = escape(text) if text else ("Loading synopsis…" if pending else "No synopsis available.")
    item_id = book.source_item_id or book.id
    url = artwork_url(item_id, full=True)
    cover = (
        f'<div class="book-details-cover"><img data-book-cover alt="" src="{escape(url, quote=True)}"></div>'
        if url else ""
    )
    title = escape(book.title)
    queue = ""
    if csrf and not book.episode_id:
        queue = (
            '<div class="actions">' + queue_book_form(book.id, csrf, "add", "Add to listening queue")
            + '<p class="hint" role="status" data-book-queue-feedback></p></div>'
        )
    panel = (
        f'<article class="book-details-panel" data-book-title="{escape(book.title, quote=True)}"'
        f' data-details-pending="{int(pending)}">{cover}<dl class="status row-dividers">{facts}</dl>{queue}'
        f'<section aria-labelledby="book-synopsis-heading"><h3 id="book-synopsis-heading">Synopsis</h3>'
        f'<p class="book-synopsis">{summary}</p></section></article>'
    )
    if fragment:
        return panel
    parent = ("Downloads", "/library?downloaded=1&section=downloads") if section == "downloads" else ("Library", "/library")
    return render_admin_page(
        f"{book.title} · Chapter", section,
        f'<header class="page-heading"><h1>{title}</h1></header><section class="divider-section">{panel}</section>',
        guest_sections=guest_sections, parent=parent,
    )
