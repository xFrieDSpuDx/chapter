"""Keep optional Audiobookshelf lookups off HTTP response threads."""

from collections import OrderedDict
from collections.abc import Callable
from threading import Lock, Thread
from time import monotonic

from .api import AudiobookshelfError


class WebReads:
    def __init__(self) -> None:
        self._lock = Lock()
        self._pending: set[tuple] = set()
        self._results: OrderedDict[tuple, tuple[float, object]] = OrderedDict()

    def get(self, key: tuple, load: Callable[[], object]) -> tuple[object, bool]:
        with self._lock:
            cached = self._results.get(key)
            if cached is not None and monotonic() < cached[0]:
                self._results.move_to_end(key)
                return cached[1], False
            previous = cached[1] if cached is not None else None
            if key in self._pending or len(self._pending) >= 2:
                return previous, True
            self._pending.add(key)
        try:
            Thread(target=self._load, args=(key, load, previous), name="chapter-web-lookup", daemon=True).start()
        except RuntimeError:
            self._finish(key, previous, 20)
            return previous, False
        return previous, True

    def _load(self, key: tuple, load: Callable[[], object], previous: object) -> None:
        result = previous
        ttl = 20
        try:
            result = load()
            ttl = 300 if key[0] == "details" else 60
        except (AudiobookshelfError, OSError, RuntimeError, ValueError):
            pass
        finally:
            self._finish(key, result, ttl)

    def _finish(self, key: tuple, result: object, ttl: float) -> None:
        with self._lock:
            self._pending.discard(key)
            self._results[key] = (monotonic() + ttl, result)
            self._results.move_to_end(key)
            while len(self._results) > 48:
                self._results.popitem(last=False)

    def pending(self, key: tuple) -> bool:
        with self._lock:
            return key in self._pending
