"""Render the browser library and the download queue."""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from .admin_layout import ASSET_DIR, TEMPLATE_DIR, cover_icon, render_admin_page, render_cover
from .config import AppConfig
from .download_queue_store import DOWNLOADED, DOWNLOADING, QUEUED, WAITING
from .download_store import DownloadedBook, completed_download_ids, web_downloads
from .download_transfer import MIB
from .downloads import downloads
from .library import series_key, sorted_books_for_menu
from .models import Book
from .player_control import WebPlayerStatus
from .setup_views import render_csrf_input
from .web_library import BROWSE_VIEWS, BookGroup, group_books
from .web_pagination import render_pagination


BOOKS_PER_PAGE = 24
GROUPED_VIEWS = {"series", "authors", "collections", "playlists", "letters", "podcasts"}
LIBRARY_PAGE = (TEMPLATE_DIR / "library.html").read_text(encoding="utf-8")
LIBRARY_RESULTS = (TEMPLATE_DIR / "library_results.html").read_text(encoding="utf-8")
LIBRARY_SCRIPTS = "".join(
    f"<script>{(ASSET_DIR / f'{name}.js').read_text(encoding='utf-8')}</script>"
    for name in ("library_filters", "downloads", "play_on_chapter")
)


def download_inventory(item_ids: list[str]) -> str:
    return sha256("\0".join(sorted(item_ids)).encode()).hexdigest()


def library_fields(
    query: str, downloaded: bool, page: int, view: str, group: str, *,
    section: str = "", hide_downloaded: bool = False, hide_finished: bool = False,
) -> dict[str, str | int]:
    fields: dict[str, str | int] = {"q": query, "downloaded": int(downloaded), "page": page}
    if section:
        fields["section"] = section
    if group or view != "titles":
        fields.update(view=view, group=group)
    if hide_downloaded and not downloaded:
        fields["hide_downloaded"] = 1
    if hide_finished:
        fields["hide_finished"] = 1
    return fields


def download_fields(query: str, downloaded: bool, page: int, view: str, group: str, **filters) -> str:
    fields = library_fields(query, downloaded, page, view, group, **filters)
    fields.update(group=group, view=view)
    return "".join(
        f'<input type="hidden" name="{key}" value="{escape(str(value), quote=True)}">'
        for key, value in fields.items()
    )


def library_url(query: str = "", downloaded: bool = False, page: int = 1, *,
                view: str = "titles", group: str = "", **filters) -> str:
    return "/library?" + urlencode(library_fields(query, downloaded, page, view, group, **filters))


def search_books(
    books: list[Book], query: str, downloaded_only: bool, *, podcasts: bool = False,
    hide_downloaded: bool = False, hide_finished: bool = False,
) -> list[Book]:
    words = query.casefold().split()
    return [
        book for book in books
        if bool(book.episode_id or book.id.startswith("podcast:")) == podcasts
        and (not downloaded_only or book.downloaded)
        and (downloaded_only or not hide_downloaded or not book.downloaded)
        and (not hide_finished or not book.finished)
        and all(word in f"{book.title} {book.author} {book.narrator} {book.podcast_title}".casefold() for word in words)
    ]


def book_count(count: int, noun: str = "book") -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def render_library(
    config: AppConfig, books: list[Book], query: str, downloaded_only: bool, page: int, csrf_token: str,
    *, view: str = "titles", group: str = "", shelves: list[BookGroup] | None = None, notice: str = "",
    section: str = "", hide_downloaded: bool = False, hide_finished: bool = False, fragment: bool = False,
    copies: Sequence[tuple[Path, DownloadedBook]] | None = None, player: WebPlayerStatus | None = None,
    catalogue_pending: bool = False, catalogue_total: int | None = None, space_remaining: int | None = None,
    guest_sections: tuple[str, ...] | None = None,
) -> str:
    can_manage = guest_sections is None
    view = view if view in BROWSE_VIEWS else "titles"
    section = section or ("downloads" if downloaded_only else "library")
    filters = {"section": section, "hide_downloaded": hide_downloaded, "hide_finished": hide_finished}
    matches = search_books(books, query, downloaded_only, podcasts=view == "podcasts",
                           hide_downloaded=hide_downloaded, hide_finished=hide_finished)
    chosen_group = None
    groups = None
    if view in GROUPED_VIEWS:
        if shelves is None:
            groups = group_books(config, matches, view)
        else:
            available = {book.id for book in matches}
            groups = [BookGroup(shelf.key, shelf.name, [book for book in shelf.books if book.id in available])
                      for shelf in shelves]
        if group:
            chosen_group = next((entry for entry in groups if entry.key == group), None)
            matches = chosen_group.books if chosen_group else []
            groups = None
        elif query or downloaded_only or hide_downloaded or hide_finished:
            groups = [shelf for shelf in groups if shelf.books]
    elif view == "titles" and not downloaded_only:
        matches = sorted_books_for_menu(matches)
    elif view == "continue":
        matches = sorted((book for book in matches if book.has_play_history), key=lambda book: -book.progress_updated_at)
    elif view == "pinned":
        matches = sorted((book for book in matches if book.pinned_position is not None),
                         key=lambda book: book.pinned_position)

    match_count = len(matches) if groups is None else len({book.id for shelf in groups for book in shelf.books})
    rows = groups if groups is not None else matches
    row_count = len(rows)
    last_page = max(1, (row_count + BOOKS_PER_PAGE - 1) // BOOKS_PER_PAGE)
    page = min(max(1, page), last_page)
    page_start = (page - 1) * BOOKS_PER_PAGE
    page_end = min(page_start + BOOKS_PER_PAGE, row_count)
    rows = rows[page_start:page_end]
    fields = ""
    if can_manage:
        fields = render_csrf_input(csrf_token) + download_fields(query, downloaded_only, page, view, group, **filters)
    noun = "episode" if view == "podcasts" else "book"
    pagination = ""
    if last_page > 1:
        row_total = f"{row_count:,} {BROWSE_VIEWS[view].lower()}" if groups is not None else book_count(row_count, noun)
        row_range = f"{page_start + 1:,}–{page_end:,}" if len(rows) > 1 else f"{page_end:,}"
        pagination = render_pagination(
            page, last_page,
            lambda target: library_url(query, downloaded_only, target, view=view, group=group, **filters),
            f"Showing {row_range} of {row_total}",
        )
    empty = "No matching titles." if config.is_ready else "Sign in to Audiobookshelf to load your books."
    done = [book.id for book in books if book.downloaded]
    if copies is not None:
        done = [record.book.id for _, record in copies if record.complete]
    if catalogue_total is None:
        catalogue_total = sum(not book.episode_id and not book.id.startswith("podcast:") and not book.download_only for book in books)
    total = f"{book_count(len(done))} downloaded" if section == "downloads" else f"{book_count(catalogue_total)} in the library"
    if groups is not None:
        tiles = "".join(render_group_tile(entry, query, downloaded_only, view, **filters) for entry in rows)
        empty = "No matching " + BROWSE_VIEWS[view].lower() + "." if config.is_ready else empty
    else:
        manifests = {record.book.id: record for _, record in copies} if copies is not None else None
        tiles = "".join(
            render_library_row(
                config, book, fields, view=view, group=group, player=player, section=section,
                can_manage=can_manage,
                download_status=downloads.status_from_manifest(config, book.id, manifests.get(book.id))
                if manifests is not None and view != "podcasts" else None,
            ) for book in rows
        )

    results_title = chosen_group.name if chosen_group else BROWSE_VIEWS[view]
    if section == "downloads" and view == "titles" and downloaded_only:
        results_title = "Downloaded books"
    results = LIBRARY_RESULTS.format(
        inventory=download_inventory(done), total=escape(total, quote=True), view=view, group=escape(group, quote=True),
        page=page,
        catalogue_pending=int(catalogue_pending),
        results_title=escape(results_title),
        matches=f"{book_count(match_count, noun)} {'matches' if match_count == 1 else 'match'}",
        back_link=(
            f'<a data-library-link class="text-action back-link" href="{escape(library_url(query, downloaded_only, view=view, **filters))}">All {BROWSE_VIEWS[view].lower()}</a>'
            if group and view in GROUPED_VIEWS else ""
        ),
        notice=f'<p class="hint" role="status">{escape(notice)}</p>' if notice else "",
        books=f'<ul class="cover-grid" role="list">{tiles}</ul>' if tiles else f'<p class="muted">{empty}</p>',
        pagination=pagination,
    )
    if fragment:
        return results

    heading = "Downloads" if section == "downloads" else "Library"
    content = LIBRARY_PAGE.format(
        heading=heading, total=total, results=results,
        queue_link='<p><a href="/player#listening-queue">Listening queue</a></p>' if can_manage else "",
        space=f'<p class="muted" data-download-space>{download_space_label(space_remaining)}</p>'
        if section == "downloads" else "",
        section=section, view=view, group=escape(group, quote=True),
        browse_options="".join(
            f'<option value="{key}"{" selected" if key == view else ""}>{label}</option>'
            for key, label in BROWSE_VIEWS.items()
        ),
        query=escape(query, quote=True), checked=" checked" if downloaded_only else "",
        hide_downloaded=" checked" if hide_downloaded and not downloaded_only else "",
        hide_finished=" checked" if hide_finished else "",
        download_queue=render_download_queue(config, books, fields, copies, can_manage=can_manage)
        if section == "downloads" else "",
    )
    return render_admin_page(f"{heading} · Chapter", section, content, scripts=LIBRARY_SCRIPTS, guest_sections=guest_sections)


def download_space_label(remaining: int | None) -> str:
    if remaining is None:
        return "Checking space for downloads…"
    return f"{remaining // MIB:,} MiB available for downloads"


QueueEntry = tuple[Book, tuple[str, int, int]]


def download_queue_entries(
    config: AppConfig, copies: Sequence[tuple[Path, DownloadedBook]], books: Sequence[Book] = (),
) -> list[QueueEntry]:
    requested = {book.id: book for book in downloads.queued_books(config)}
    manifests = {record.book.id: record for _, record in copies}
    for manifest in manifests.values():
        if not manifest.complete:
            requested.setdefault(manifest.book.id, manifest.book)
    catalogue = {book.id: book for book in books}
    entries = []
    for item_id, book in requested.items():
        status = downloads.status_from_manifest(config, item_id, manifests.get(item_id))
        if status[0] and status[0] != DOWNLOADED:
            entries.append((catalogue.get(item_id, book), status))
    return entries


def queue_revision(entries: Sequence[QueueEntry], fields: str) -> str:
    identifiers = "\0".join(book.id for book, _ in entries)
    return sha256(f"{fields}\0{identifiers}".encode()).hexdigest()


def queue_count_label(count: int) -> str:
    return f"Download queue ({count:,})"


def render_download_queue(
    config: AppConfig, books: list[Book], fields: str, copies: Sequence[tuple[Path, DownloadedBook]] | None = None,
    *, can_manage: bool = True,
) -> str:
    copies = web_downloads(config) if copies is None else copies
    entries = download_queue_entries(config, copies, books)
    rows = render_download_queue_rows(fields, entries, can_manage=can_manage)
    completed = completed_download_ids(copies)
    count = downloads.activity(config, completed).in_queue
    return (
        f'<section class="divider-section" aria-labelledby="download-queue" data-download-queue data-queue-revision="{queue_revision(entries, fields)}">'
        f'<h2 id="download-queue" data-download-queue-heading>{queue_count_label(count)}</h2>'
        f'<ul class="download-queue-list row-dividers" role="list" data-download-queue-list{"" if rows else " hidden"}>{rows}</ul>'
        f'<p class="muted" data-download-queue-empty{" hidden" if rows else ""}>No books waiting to download.</p>'
        "</section>"
    )


def render_download_queue_rows(fields: str, entries: Sequence[QueueEntry], *, can_manage: bool = True) -> str:
    return "".join(
        render_download_queue_row(book, fields, download_card_state(*status, can_manage=can_manage, title=book.title))
        for book, status in entries
    )


def render_group_tile(entry: BookGroup, query: str, downloaded: bool, view: str, **filters) -> str:
    url = escape(library_url(query, downloaded, view=view, group=entry.key, **filters), quote=True)
    cover_id = (entry.books[0].source_item_id or entry.books[0].id) if entry.books else ""
    count = book_count(len(entry.books), "episode" if view == "podcasts" else "book")
    return (
        f'<li class="cover-tile"><a data-library-link class="group-tile" href="{url}">{render_cover(cover_id)}'
        f'<strong>{escape(entry.name)}</strong><small>{count}</small></a></li>'
    )


def download_card_state(status: str, downloaded: int, total: int, *, can_manage: bool = True, title: str = "") -> dict[str, str]:
    if status in (QUEUED, DOWNLOADING, WAITING):
        actions = (("cancel", "Cancel download", "cancel"),)
    elif status == DOWNLOADED:
        actions = (("remove", "Remove download", "trash"),)
    elif status:
        actions = (("download", "Retry download", "retry"), ("remove", "Remove download", "trash"))
    else:
        actions = (("download", "Download", "download"),)
    title_suffix = ", " + escape(title, quote=True) if title else ""
    buttons = "".join(
        f'<button type="submit" class="cover-button" name="action" value="{action}"'
        f' aria-label="{label}{title_suffix}" title="{label}">'
        f'{cover_icon(icon)}</button>'
        for action, label, icon in actions if can_manage
    )
    progress = ""
    if status == DOWNLOADING or (status == WAITING and downloaded > 0):
        values = f' max="{total}" value="{downloaded}"' if total > 0 else ""
        progress = f'<progress class="tile-progress"{values} aria-label="Download progress"></progress>'
    elif status in (QUEUED, WAITING):
        progress = f'<span class="download-waiting" role="img" aria-label="{escape(status)}"></span>'
    label = "" if status == DOWNLOADED else status
    if status == DOWNLOADING and total:
        label = f"Downloading · {downloaded / total:.0%}"
    return {"label": label, "progress": progress, "buttons": buttons}


def render_book_details_link(book: Book, section: str) -> str:
    fields = {"id": book.id}
    if section == "downloads":
        fields["section"] = section
    url = escape("/library/details?" + urlencode(fields), quote=True)
    return (
        f'<a class="cover-button cover-info" href="{url}" data-book-details aria-label="Book details, {escape(book.title, quote=True)}"'
        f' title="Book details" data-book-title="{escape(book.title, quote=True)}">{cover_icon("info")}</a>'
    )


def render_download_form(item_id: str, fields: str, buttons: str) -> str:
    return (
        f'<form action="/library/download" method="post" class="cover-download" data-download-form>{fields}'
        f'<input type="hidden" name="book_id" value="{item_id}">'
        f'<div class="cover-buttons" data-download-buttons>{buttons}</div></form>'
    )


def render_download_progress(progress: str) -> str:
    return f'<div data-download-progress{"" if progress else " hidden"}>{progress}</div>'


def render_download_status(label: str) -> str:
    return (
        f'<p class="muted" data-download-label{"" if label else " hidden"}>{escape(label)}</p>'
        '<p class="hint" role="status" data-download-error hidden></p>'
    )


def render_download_queue_row(book: Book, fields: str, state: dict[str, str]) -> str:
    item_id = escape(book.id, quote=True)
    form = render_download_form(item_id, fields, state["buttons"]) if state["buttons"] else ""
    return (
        f'<li class="download-queue-row" data-download-id="{item_id}">'
        f'<div class="cover-with-controls">{render_cover(book.source_item_id or book.id)}{render_book_details_link(book, "downloads")}</div>'
        f'<div class="book-details"><strong>{escape(book.title)}</strong>{render_download_status(state["label"])}'
        f'<div class="tile-progress-slot">{render_download_progress(state["progress"])}</div></div>'
        f'{form}</li>'
    )


def render_library_row(
    config: AppConfig, book: Book, fields: str, *, view: str = "titles", group: str = "",
    download_status: tuple[str, int, int] | None = None, player: WebPlayerStatus | None = None,
    can_manage: bool = True, section: str = "library",
) -> str:
    is_episode = view == "podcasts"
    state = None
    if not is_episode:
        state = download_card_state(*(download_status or downloads.status(config, book.id)), can_manage=can_manage, title=book.title)
    item_id = escape(book.id, quote=True)
    overlay = progress = feedback = status = ""
    if state:
        overlay = render_book_details_link(book, section)
        if state["buttons"]:
            overlay += render_download_form(item_id, fields, state["buttons"])
        progress = render_download_progress(state["progress"])
        status = render_download_status(state["label"])
    if state and config.is_ready and can_manage:
        playing = player and player.cover_item_id == book.id and player.is_playing
        label = "Pause on Chapter" if playing else "Play on Chapter"
        action = "pause_book" if playing else "play_book"
        overlay += (
            f'<form action="/player" method="post" class="cover-play" data-play-on-chapter>{fields}'
            f'<input type="hidden" name="action" value="{action}">'
            f'<input type="hidden" name="book_id" value="{item_id}">'
            f'<button type="submit" class="cover-button" aria-label="{label}, {escape(book.title, quote=True)}" title="{label}">'
            f'{cover_icon("pause" if playing else "play")}</button></form>'
        )
        feedback = '<p class="hint" role="status" data-play-feedback></p>'

    if book.has_play_history:
        hidden = " hidden" if state and state["progress"] else ""
        progress += (
            f'<progress class="tile-progress" data-listening-progress max="1" value="{min(1, max(0, book.progress))}"'
            f' aria-label="Listening progress"{hidden}></progress>'
        )

    sequence = next((member.sequence for member in book.series if series_key(member) == group), "") if view == "series" else ""
    byline = " · ".join(value for value in (book.author, f"Book {sequence}" if sequence else "") if value)
    details = f'<p class="muted book-byline">{escape(byline)}</p>' if byline else ""
    item = f' data-download-id="{item_id}"' if state else ""
    cover = render_cover(book.source_item_id or book.id)
    cover_feedback = f'<div class="cover-feedback">{status}{feedback}</div>' if status or feedback else ""
    return (
        f'<li class="cover-tile"{item}><div class="tile-artwork">'
        f'<div class="cover-with-controls">{cover}{overlay}{cover_feedback}</div>'
        f'<div class="tile-progress-slot">{progress}</div></div>'
        f'<div class="book-details"><strong>{escape(book.title)}</strong>{details}</div></li>'
    )
