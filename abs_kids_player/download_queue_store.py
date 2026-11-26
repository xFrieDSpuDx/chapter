"""Persist the ordered download requests for one Audiobookshelf account."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .download_store import download_book_from_json
from .models import Book
from .storage import write_json_atomically


QUEUED = "Queued"
WAITING = "Waiting to retry"
DOWNLOADING = "Downloading"
CANCELLED = "Cancelled"
FAILED = "Failed"
DOWNLOADED = "Downloaded"
RUNNABLE_STATES = {QUEUED, WAITING}
STORED_STATES = {*RUNNABLE_STATES, CANCELLED, FAILED}


@dataclass(frozen=True)
class DownloadRequest:
    book: Book
    state: str = QUEUED
    message: str = ""


def read_download_queue(directory: Path) -> dict[str, DownloadRequest]:
    try:
        rows = json.loads((directory / "queue.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(rows, list):
        return {}

    requests = {}
    for row in rows:
        try:
            book = download_book_from_json(row["book"])
            state = row["state"]
            if isinstance(book.id, str) and book.id and state in STORED_STATES:
                requests[book.id] = DownloadRequest(book, state, str(row.get("message") or ""))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return requests


def save_download_queue(directory: Path, requests: dict[str, DownloadRequest]) -> None:
    write_json_atomically(
        directory / "queue.json",
        [asdict(request) for request in requests.values()],
        compact=True,
        durable=True,
    )
