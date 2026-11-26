from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, replace
from functools import lru_cache
from pathlib import Path

from .config import AppConfig
from .models import Book, SeriesMembership
from .storage import write_json_atomically


LIBRARY_CACHE_PATH = Path.home() / ".local" / "state" / "abs-kids-player" / "library-cache.json"
LIBRARY_CACHE_VERSION = 1


def _cached_books(config: AppConfig) -> list[Book]:
    if not config.is_ready:
        return []

    path = library_cache_path()
    try:
        stat = path.stat()
        return _read_library_cache(
            path, config_cache_key(config), (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        )
    except (OSError, ValueError):
        return []


def load_cached_books(config: AppConfig) -> list[Book]:
    # Browsing overlays current playback progress on these copies.
    return [replace(book) for book in _cached_books(config)]


def cached_download_progress(config: AppConfig, downloaded: Book) -> tuple[float, int]:
    """Return the selected book's catalogue progress, falling back to its download manifest."""
    for book in _cached_books(config):
        if book.id == downloaded.id and book.progress_updated_at >= downloaded.progress_updated_at:
            return book.current_time, book.progress_updated_at
    return downloaded.current_time, downloaded.progress_updated_at


@lru_cache(maxsize=4)
def _read_library_cache(path: Path, key: str, revision: tuple[int, int, int]) -> list[Book]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(payload, dict):
        return []
    if payload.get("version") != LIBRARY_CACHE_VERSION:
        return []
    if payload.get("config_key") != key:
        return []

    books: list[Book] = []
    for value in payload.get("books", []):
        if not isinstance(value, dict):
            continue
        try:
            books.append(
                Book(
                    id=str(value["id"]),
                    title=str(value["title"]),
                    author=str(value.get("author", "")),
                    duration=float(value.get("duration", 0)),
                    cover_url="",
                    current_time=float(value.get("current_time", 0)),
                    progress=float(value.get("progress", 0)),
                    series_name=str(value.get("series_name", "")),
                    series_sequence=str(value.get("series_sequence", "")),
                    series_total=int(value.get("series_total", 0)),
                    series_memberships=tuple(
                        SeriesMembership(**membership)
                        for membership in value.get("series_memberships", [])
                        if isinstance(membership, dict)
                    ),
                    progress_updated_at=int(value.get("progress_updated_at", 0)),
                    is_finished=bool(value.get("is_finished", False)),
                    pinned_position=value.get("pinned_position"),
                    episode_id=str(value.get("episode_id") or ""),
                    podcast_title=str(value.get("podcast_title") or ""),
                    source_item_id=str(value.get("source_item_id") or ""),
                    narrator=str(value.get("narrator") or ""),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return books


def save_cached_books(config: AppConfig, books: list[Book]) -> bool:
    if not config.is_ready:
        return False

    path = library_cache_path()
    payload = {
        "version": LIBRARY_CACHE_VERSION,
        "config_key": config_cache_key(config),
        "books": [
            {
                "id": book.id,
                "title": book.title,
                "author": book.author,
                "duration": book.duration,
                "current_time": book.current_time,
                "progress": book.progress,
                "series_name": book.series_name,
                "series_sequence": book.series_sequence,
                "series_total": book.series_total,
                "series_memberships": [asdict(member) for member in book.series_memberships],
                "progress_updated_at": book.progress_updated_at,
                "is_finished": book.is_finished,
                "pinned_position": book.pinned_position,
                "episode_id": book.episode_id,
                "podcast_title": book.podcast_title,
                "source_item_id": book.source_item_id,
                "narrator": book.narrator,
            }
            for book in books
        ],
    }
    try:
        write_json_atomically(path, payload, compact=True)
    except OSError as error:
        print(f"Library cache could not be saved: {error}")
        return False
    return True


def clear_cached_books() -> None:
    try:
        library_cache_path().unlink(missing_ok=True)
    except OSError:
        pass


def config_cache_key(config: AppConfig) -> str:
    identity = {
        "server_url": config.server_url.rstrip("/"),
        "library_id": config.library_id,
        "username": config.username,
        "podcasts": [asdict(podcast) for podcast in config.podcasts],
    }
    if config.pinned_shelf:
        identity["pinned_shelf"] = config.pinned_shelf
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def library_cache_path() -> Path:
    configured_path = os.environ.get("ABS_KIDS_PLAYER_LIBRARY_CACHE_PATH", "").strip()
    if configured_path:
        return Path(configured_path).expanduser()
    configured_dir = os.environ.get("ABS_KIDS_PLAYER_STATE_DIR", "").strip()
    if configured_dir:
        return Path(configured_dir).expanduser() / LIBRARY_CACHE_PATH.name
    return LIBRARY_CACHE_PATH
