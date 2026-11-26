"""Read and publish complete, account-scoped audiobook downloads."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import stat
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path
from threading import Lock
from time import monotonic

from .config import AppConfig
from .library_cache import cached_download_progress
from .models import AudioTrack, Book, Chapter, PlaybackSession, SeriesMembership
from .player_state import state_path
from .progress_sync import PendingProgress, progress_account, read_listening_progress
from .storage import finalize_storage_file, write_json_atomically


MANIFEST_NAME = "book.json"


@dataclass(frozen=True)
class DownloadedBook:
    book: Book
    tracks: list[AudioTrack]
    chapters: list[Chapter]
    complete: bool = False
    error: str = ""

    @property
    def total_bytes(self) -> int:
        return sum(track.size_bytes or 0 for track in self.tracks)

    def save(self, directory: Path) -> None:
        write_json_atomically(directory / MANIFEST_NAME, asdict(self), durable=True)
        invalidate_web_downloads()

    def local_session(self, directory: Path, account: str, position: float, updated_at: int = 0) -> PlaybackSession:
        return PlaybackSession(
            "", self.book.id, self.book.title, self.book.author, self.book.duration,
            min(max(0.0, position), self.book.duration), "",
            [replace(track, url=(directory / track.url).as_uri()) for track in self.tracks],
            self.chapters, progress_account=account, downloaded=True,
            download_directory=str(directory), progress_updated_at=updated_at,
        )


def download_root() -> Path:
    return state_path().parent.absolute() / "downloads"


def account_directory(config: AppConfig) -> Path:
    return download_root() / progress_account(config)


def book_directory(config: AppConfig, item_id: str) -> Path:
    key = hashlib.sha256(item_id.encode()).hexdigest()
    return account_directory(config) / key


def download_book_from_json(data: dict) -> Book:
    book = Book(**data)
    book.series_memberships = tuple(SeriesMembership(**row) for row in book.series_memberships)
    return book


def lock_download(directory: Path, *, shared: bool):
    path = directory.with_suffix(".lock")
    stream = path.open("a+b")
    try:
        finalize_storage_file(path)
        fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise
    return stream


def read_download(directory: Path) -> DownloadedBook | None:
    try:
        data = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
        book = download_book_from_json(data["book"])
        tracks = [AudioTrack(**row) for row in data["tracks"]]
        if not tracks or any(
            track.url != f"track-{index}" or not track.size_bytes or track.size_bytes < 1
            for index, track in enumerate(tracks, 1)
        ):
            return None
        chapters = [Chapter(**row) for row in data["chapters"]]
        complete = bool(data["complete"]) and all(
            file_size(directory / track.url) == track.size_bytes for track in tracks
        )
        return DownloadedBook(book, tracks, chapters, complete, str(data.get("error") or ""))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def file_size(path: Path) -> int:
    try:
        info = path.stat()
        return info.st_size if stat.S_ISREG(info.st_mode) else 0
    except OSError:
        return 0


def account_downloads(config: AppConfig) -> list[tuple[Path, DownloadedBook]]:
    return _scan_downloads(account_directory(config)) if config.is_ready else []


def _scan_downloads(account: Path) -> list[tuple[Path, DownloadedBook]]:
    return [
        (path, download) for path in account.glob("*/")
        if (download := read_download(path))
        and path.name == hashlib.sha256(download.book.id.encode()).hexdigest()
    ]


_web_revision = 0
_web_index_lock = Lock()


def invalidate_web_downloads() -> None:
    global _web_revision
    with _web_index_lock:
        _web_revision += 1


@lru_cache(maxsize=6)
def _web_downloads_at(account: Path, revision: int, interval: int) -> tuple[tuple[Path, DownloadedBook], ...]:
    return tuple(_scan_downloads(account))


def web_downloads(config: AppConfig) -> tuple[tuple[Path, DownloadedBook], ...]:
    if not config.is_ready:
        return ()
    with _web_index_lock:
        revision = _web_revision
    return _web_downloads_at(account_directory(config), revision, int(monotonic() // 5))


def completed_download_ids(copies: Sequence[tuple[Path, DownloadedBook]]) -> set[str]:
    return {record.book.id for _, record in copies if record.complete}


def books_with_downloads(
    config: AppConfig, books: list[Book], *, include_incomplete: bool = False,
    copies: Sequence[tuple[Path, DownloadedBook]] | None = None,
    positions: dict[tuple[str, str, str], PendingProgress] | None = None,
) -> list[Book]:
    copies = account_downloads(config) if copies is None else copies
    ready = {download.book.id: download.book for _, download in copies if download.complete}
    result = {}
    for book in books:
        downloaded = ready.pop(book.id, None)
        result[book.id] = replace(book, downloaded=bool(downloaded))
    result.update({item_id: replace(book, downloaded=True, download_only=True) for item_id, book in ready.items()})
    if include_incomplete:
        for _, download in copies:
            result.setdefault(download.book.id, replace(download.book))
    if not any(book.downloaded for book in result.values()):
        return list(result.values())
    if positions is None:
        positions = read_listening_progress()
    account = progress_account(config)
    for book in result.values():
        progress = positions.get((account, book.id, ""))
        if book.downloaded and progress and progress.updated_at >= book.progress_updated_at:
            book.current_time = min(progress.current_time, book.duration)
            book.progress_updated_at = progress.updated_at
            book.progress = book.current_time / book.duration if book.duration else 0
            book.is_finished = book.progress >= 0.995
    return list(result.values())


def local_book_position(
    config: AppConfig, book: Book, fallback: float, *, start_over: bool, live: tuple[float, int] | None = None,
) -> tuple[float, int]:
    if start_over:
        # ProgressSync already orders local edits. Keep the known server timestamp
        # so the restart is newer even when the Pi's clock is behind.
        return 0.0, cached_download_progress(config, book)[1]
    progress = read_listening_progress().get((progress_account(config), book.id, ""))
    if live is not None:
        if progress and not progress.synced and progress.updated_at > live[1]:
            return progress.current_time, progress.updated_at
        return live
    position, updated_at = cached_download_progress(config, book)
    if progress and progress.updated_at >= updated_at:
        position, updated_at = progress.current_time, progress.updated_at
    if book.duration > 0 and position >= 0.995 * book.duration:
        return 0.0, updated_at
    if updated_at or position:
        return position, updated_at
    return (fallback if math.isfinite(fallback) else 0.0), 0
