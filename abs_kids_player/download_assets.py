"""Store a downloaded book's synopsis and artwork beside its audio."""

from __future__ import annotations

import json
import os
import tempfile
from collections import OrderedDict
from http.client import HTTPException
from io import BytesIO
from pathlib import Path
from threading import Lock, Thread
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import quote

from .api import AudiobookshelfClient, AudiobookshelfError, client_from_config
from .config import AppConfig
from .download_store import book_directory, lock_download, read_download, web_downloads
from .storage import finalize_storage_file, write_json_atomically


DETAILS_NAME = "details.json"
IMAGE_FILENAMES = {
    "image/jpeg": "cover.jpg", "image/png": "cover.png", "image/webp": "cover.webp",
    "image/gif": "cover.gif", "image/avif": "cover.avif",
}
MAX_ART_BYTES = 2 * 1024 * 1024
THUMBNAIL_NAME = 'cover.thumb.jpg'
DOWNLOAD_COVER_WIDTH = 800


def select_details(item: object) -> dict:
    media = item.get("media") if isinstance(item, dict) else None
    metadata = media.get("metadata") if isinstance(media, dict) else None
    if not isinstance(metadata, dict):
        return {}
    limits = {"description": 65536, "publisher": 256, "publishedYear": 32}
    selected = {
        field: str(metadata[field])[:limit] for field, limit in limits.items()
        if isinstance(metadata.get(field), (str, int))
    }
    genres = metadata.get("genres")
    if isinstance(genres, list):
        selected["genres"] = [entry[:100] for entry in genres[:32] if isinstance(entry, str)]
    return selected


def save_details(directory: Path, details: dict) -> None:
    write_json_atomically(directory / DETAILS_NAME, details, compact=True, durable=True)


def read_details(directory: Path) -> dict | None:
    try:
        record = json.loads((directory / DETAILS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def read_saved_cover(directory: Path, *, full: bool = True) -> tuple[str, bytes] | None:
    filenames = list(IMAGE_FILENAMES.items())
    if not full:
        filenames.insert(0, ("image/jpeg", THUMBNAIL_NAME))
    for mime, name in filenames:
        try:
            with (directory / name).open("rb") as stream:
                data = stream.read(MAX_ART_BYTES + 1)
        except OSError:
            continue
        if 0 < len(data) <= MAX_ART_BYTES:
            return mime, data
    return None


def has_saved_cover(directory: Path) -> bool:
    return any((directory / name).is_file() for name in IMAGE_FILENAMES.values())


def save_cover(directory: Path, cover: tuple[str, bytes]) -> None:
    mime, data = cover
    filename = IMAGE_FILENAMES.get(mime)
    if filename is None or not 0 < len(data) <= MAX_ART_BYTES:
        return
    _save_image(directory / filename, data)
    thumbnail = cover_thumbnail(data)
    if thumbnail:
        _save_image(directory / THUMBNAIL_NAME, thumbnail)


def cover_thumbnail(data: bytes) -> bytes | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 12_000_000:
                return None
            image.thumbnail((320, 320))
            output = BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=82)
            return output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _save_image(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            finalize_storage_file(temporary)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def request_cover(
    client: AudiobookshelfClient, item_id: str, *, width: int, limit: int, timeout: float = 2,
) -> tuple[str, bytes] | None:
    path = f"/api/items/{quote(item_id, safe='')}/cover?width={width}&format=webp"
    try:
        with client.open_download(path, timeout=timeout) as response:
            mime = response.headers.get_content_type()
            image = response.read(limit + 1) if mime in IMAGE_FILENAMES else b""
    except HTTPError as error:
        if error.fp is not None:
            error.close()
        return None
    except (OSError, ValueError, HTTPException, AudiobookshelfError):
        return None
    return (mime, image) if 0 < len(image) <= limit else None


def save_download_cover(client: AudiobookshelfClient, directory: Path, item_id: str) -> None:
    if not has_saved_cover(directory):
        cover = request_cover(client, item_id, width=DOWNLOAD_COVER_WIDTH, limit=MAX_ART_BYTES)
        if cover:
            save_cover(directory, cover)


class DownloadAssetBackfill:
    """Fill missing assets on existing downloads, one book per pass."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._working = False
        self._next_check = 0.0
        self._retry_at: OrderedDict[Path, float] = OrderedDict()

    def schedule(self, config: AppConfig) -> None:
        with self._lock:
            if not config.is_ready or self._working or monotonic() < self._next_check:
                return
            self._working = True
        Thread(target=self._run, args=(config,), name="chapter-download-assets", daemon=True).start()

    def _run(self, config: AppConfig) -> None:
        try:
            self.step(config)
        finally:
            with self._lock:
                self._working = False

    def step(self, config: AppConfig) -> None:
        now = monotonic()
        if not config.is_ready or now < self._next_check:
            return
        self._next_check = now + 30
        for directory, manifest in web_downloads(config):
            if not manifest.complete or now < self._retry_at.get(directory, 0):
                continue
            details_missing = not (directory / DETAILS_NAME).is_file()
            cover_missing = not has_saved_cover(directory)
            if not details_missing and not cover_missing:
                continue
            self._next_check = now + 5
            if not self._backfill(config, manifest.book.id, details_missing, cover_missing):
                self._retry_at[directory] = now + 300
                self._retry_at.move_to_end(directory)
                while len(self._retry_at) > 256:
                    self._retry_at.popitem(last=False)
            return

    @staticmethod
    def _backfill(config: AppConfig, item_id: str, fetch_details: bool, fetch_cover: bool) -> bool:
        directory = book_directory(config, item_id)
        client = client_from_config(config)
        details = cover = None
        if fetch_details:
            try:
                item = client.request("GET", f"/api/items/{quote(item_id, safe='')}", query={"expanded": 1})
                details = select_details(item)
            except (AudiobookshelfError, OSError, HTTPException, ValueError):
                pass
        if fetch_cover:
            cover = request_cover(client, item_id, width=DOWNLOAD_COVER_WIDTH, limit=MAX_ART_BYTES)
        try:
            with lock_download(directory, shared=True):
                record = read_download(directory)
                if record is None or not record.complete:
                    return True
                if details is not None:
                    save_details(directory, details)
                if cover:
                    save_cover(directory, cover)
        except OSError:
            return False
        return (not fetch_details or details is not None) and (not fetch_cover or cover is not None)
