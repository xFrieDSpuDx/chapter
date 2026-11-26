"""Serve bounded Audiobookshelf artwork and prefer a downloaded book's saved cover."""

from collections import OrderedDict
from re import fullmatch
from threading import BoundedSemaphore, Lock
from time import monotonic
from urllib.parse import quote

from .api import client_from_config
from .config import AppConfig
from .download_assets import DOWNLOAD_COVER_WIDTH, MAX_ART_BYTES, read_saved_cover, request_cover
from .download_store import book_directory
from .progress_sync import progress_account


MAX_COVER_BYTES = 512 * 1024
MAX_CACHED_COVERS = 32
COVER_TTL = 30 * 60
COVER_RETRY_SECONDS = 20
_cache: OrderedDict[tuple[str, str, int], tuple[float, tuple[str, bytes]]] = OrderedDict()
_retry_at: OrderedDict[tuple[str, str, int], float] = OrderedDict()
_pending: set[tuple[str, str, int]] = set()
_lock = Lock()
_requests = BoundedSemaphore(4)


def artwork_url(item_id: str, *, full: bool = False) -> str:
    if not fullmatch(r"[A-Za-z0-9_-]{1,160}", item_id):
        return ""
    return f"/artwork/{quote(item_id, safe='')}" + ("?size=detail" if full else "")


def fetch_artwork(config: AppConfig, item_id: str, *, full: bool = False) -> tuple[str, bytes] | None:
    if not config.is_ready or not artwork_url(item_id):
        return None
    width = DOWNLOAD_COVER_WIDTH if full else 320
    key = (progress_account(config), item_id, width)
    with _lock:
        old = _cache.get(key)
        if old and monotonic() - old[0] < COVER_TTL:
            _cache.move_to_end(key)
            return old[1]
    if saved := read_saved_cover(book_directory(config, item_id), full=full):
        return saved
    with _lock:
        if key in _pending or monotonic() < _retry_at.get(key, 0):
            return old[1] if old else None
        _pending.add(key)

    try:
        if not _requests.acquire(timeout=1):
            return old[1] if old else None
        try:
            image = request_cover(client_from_config(config), item_id, width=width,
                                  limit=MAX_ART_BYTES if full else MAX_COVER_BYTES)
        finally:
            _requests.release()
        with _lock:
            if image:
                _cache[key] = (monotonic(), image)
                _cache.move_to_end(key)
                while len(_cache) > MAX_CACHED_COVERS:
                    _cache.popitem(last=False)
            else:
                _retry_at[key] = monotonic() + COVER_RETRY_SECONDS
                _retry_at.move_to_end(key)
                while len(_retry_at) > 128:
                    _retry_at.popitem(last=False)
        return image or (old[1] if old else None)
    finally:
        with _lock:
            _pending.discard(key)
