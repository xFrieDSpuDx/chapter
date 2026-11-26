"""Serve download actions and status after the setup server checks access."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .config import AppConfig, load_config, update_config, valid_download_limit
from .download_space import DownloadSpace
from .download_store import (
    DownloadedBook,
    book_directory,
    completed_download_ids,
    read_download,
    web_downloads,
)
from .download_transfer import DownloadError
from .download_views import (
    BOOKS_PER_PAGE,
    download_card_state,
    download_fields,
    download_inventory,
    download_queue_entries,
    download_space_label,
    queue_count_label,
    queue_revision,
    render_download_queue_rows,
)
from .downloads import downloads
from .models import Book
from .player_control import load_web_player_status
from .setup_http import one
from .setup_views import render_csrf_input
from .web_library import BROWSE_VIEWS, find_library_book, library_display_filters, library_page


if TYPE_CHECKING:
    from .setup_server import SetupHandler


def change_download_settings(request: SetupHandler) -> None:
    fields = request.post_fields()
    if fields is None:
        return
    update_config(
        lambda current: replace(
            current,
            prefer_downloaded=one(fields, "prefer_downloaded") == "1",
            download_limit_mib=valid_download_limit(one(fields, "download_limit_mib")),
        )
    )
    if urlparse(request.path).path == "/settings/downloads":
        request.redirect_settings("downloads")
    else:
        request.redirect_library(fields)


def _book_to_download(config: AppConfig, item_id: str) -> Book | None:
    book = find_library_book(config, item_id)
    if book is not None or not item_id:
        return book
    record = read_download(book_directory(config, item_id))
    if record:
        return record.book
    return next((book for book in downloads.queued_books(config) if book.id == item_id), None)


def change_download(request: SetupHandler, space: DownloadSpace) -> None:
    fields = request.post_fields()
    if fields is None:
        return
    config = load_config()
    if not config.is_ready:
        request.send_request_error(409, "Sign in to Audiobookshelf before managing downloads.")
        return
    item_id, action = one(fields, "book_id"), one(fields, "action")
    wants_json = getattr(request, "headers", {}).get("X-Requested-With") == "fetch"
    try:
        if action == "download":
            book = _book_to_download(config, item_id)
            if book is None:
                request.send_request_error(400, "Choose a book from the current library.")
                return
            downloads.start(config, book)
        elif action == "cancel" and item_id:
            downloads.cancel(config, item_id)
        elif action == "remove" and item_id:
            downloads.remove(config, item_id)
            space.invalidate()
        else:
            request.send_request_error(400, "Choose a book from the current library.")
            return
        card = download_card_state(*downloads.status(config, item_id)) if wants_json else None
    except DownloadError as error:
        request.send_request_error(409, str(error))
        return
    except OSError:
        request.send_request_error(409, "Could not update the download. Check the device storage.")
        return
    if card is None:
        request.redirect_library(fields)
    else:
        request.send_json({"card": card})


def download_status_payload(
    config: AppConfig,
    fields: dict[str, list[str]],
    *,
    csrf: str | None,
    space: DownloadSpace,
) -> dict:
    copies = web_downloads(config)
    manifests = {record.book.id: record for _, record in copies}
    can_manage = csrf is not None
    cards = {
        item_id: download_card_state(
            *downloads.status_from_manifest(config, item_id, manifests.get(item_id)),
            can_manage=can_manage,
        )
        for item_id in list(dict.fromkeys(fields.get("id", [])))[:BOOKS_PER_PAGE]
    }
    payload = {"cards": cards}
    if can_manage:
        player = load_web_player_status()
        payload.update(
            csrf=csrf, player={"itemId": player.cover_item_id, "isPlaying": player.is_playing}
        )
    filters = library_display_filters(fields)
    downloaded_only = one(fields, "downloaded") == "1"
    if filters["section"] == "downloads" or downloaded_only or filters["hide_downloaded"]:
        completed = completed_download_ids(copies)
        payload["inventory"] = download_inventory(list(completed))
        if filters["section"] == "downloads":
            reading = space.reading(config)
            payload["space"] = download_space_label(reading.remaining)
            payload["spaceUpdating"] = reading.updating
            payload["queueHeading"] = queue_count_label(
                downloads.activity(config, completed).in_queue
            )
            _add_queue_updates(payload, config, fields, copies, csrf)
    return payload


def _add_queue_updates(
    payload: dict,
    config: AppConfig,
    fields: dict[str, list[str]],
    copies: Sequence[tuple[Path, DownloadedBook]],
    csrf: str | None,
) -> None:
    controls = ""
    if csrf is not None:
        view = one(fields, "view")
        controls = render_csrf_input(csrf) + download_fields(
            one(fields, "q")[:200],
            one(fields, "downloaded") == "1",
            library_page(fields),
            view if view in BROWSE_VIEWS else "titles",
            one(fields, "group")[:240],
            **library_display_filters(fields),
        )
    entries = download_queue_entries(config, copies)
    revision = queue_revision(entries, controls)
    if revision != one(fields, "queue_revision"):
        payload["queue"] = render_download_queue_rows(
            controls, entries, can_manage=csrf is not None
        )
        payload["queueRevision"] = revision
    else:
        payload["cards"].update(
            (book.id, download_card_state(*status, can_manage=csrf is not None))
            for book, status in entries
        )
