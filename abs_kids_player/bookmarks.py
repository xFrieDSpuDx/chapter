"""Read Audiobookshelf bookmarks without blocking the OLED or playback owner."""

from __future__ import annotations

import math
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from http.client import HTTPException

from .api import AudiobookshelfError, client_from_config
from .config import AppConfig
from .models import PlaybackSession
from .progress_sync import progress_account


@dataclass(frozen=True)
class Bookmark:
    time: float
    title: str


def read_bookmarks(config: AppConfig, session: PlaybackSession) -> list[Bookmark]:
    account = progress_account(config)
    if not account or session.progress_account != account or session.episode_id:
        raise AudiobookshelfError("The current book does not belong to the signed-in account.")
    return bookmarks_for_item(config, session.library_item_id, session.duration)


def bookmarks_for_item(config: AppConfig, item_id: str, duration: float) -> list[Bookmark]:
    rows = client_from_config(config).get_current_user().get("bookmarks", [])
    bookmarks = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("libraryItemId") != item_id:
            continue
        position = row.get("time")
        if type(position) in (int, float) and 0 <= position < duration and math.isfinite(position):
            title = row.get("title")
            bookmarks[position] = Bookmark(position, title.strip() if isinstance(title, str) else "")
    return sorted(bookmarks.values(), key=lambda bookmark: bookmark.time)


def request_bookmarks(config: AppConfig, session: PlaybackSession) -> Future[list[Bookmark] | None]:
    result: Future[list[Bookmark] | None] = Future()

    def fetch() -> None:
        try:
            result.set_result(read_bookmarks(config, session))
        except (AudiobookshelfError, HTTPException, OSError, ValueError, OverflowError):
            result.set_result(None)

    threading.Thread(target=fetch, name="chapter-bookmarks", daemon=True).start()
    return result
