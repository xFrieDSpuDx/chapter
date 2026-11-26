"""Populate an empty browser catalogue without blocking page requests."""

from collections import OrderedDict
from threading import Lock, Thread
from time import monotonic

from .api import AudiobookshelfError
from .catalogue_settings import catalogue_request_key
from .config import AppConfig, load_config
from .library import load_library_books
from .library_cache import save_cached_books


class CatalogueLoader:
    def __init__(self) -> None:
        self._lock = Lock()
        self._working: set[str] = set()
        self._recent: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def request(self, config: AppConfig) -> str:
        key = catalogue_request_key(config)
        with self._lock:
            if key in self._working:
                return "loading"
            recent = self._recent.get(key)
            if recent and monotonic() < recent[0]:
                return recent[1]
            self._working.add(key)
        try:
            Thread(target=self._load, args=(config, key), name="chapter-web-catalogue", daemon=True).start()
        except RuntimeError:
            self._finish(key, "failed")
            return "failed"
        return "loading"

    def _load(self, config: AppConfig, key: str) -> None:
        result = "failed"
        try:
            books = load_library_books(config)
            if key == catalogue_request_key(load_config()):
                result = "ready" if save_cached_books(config, books) else "save_failed"
        except (AudiobookshelfError, OSError, ValueError):
            pass
        finally:
            self._finish(key, result)

    def _finish(self, key: str, result: str) -> None:
        with self._lock:
            self._working.remove(key)
            self._recent[key] = (monotonic() + 30, result)
            self._recent.move_to_end(key)
            while len(self._recent) > 8:
                self._recent.popitem(last=False)

    def pending(self, config: AppConfig) -> bool:
        with self._lock:
            return catalogue_request_key(config) in self._working
