"""Read slow device status in the background while callers use the last sample."""

from collections.abc import Callable
from threading import Lock, Thread
from time import monotonic
from typing import Generic, TypeVar


T = TypeVar("T")


class DeviceStatusCache(Generic[T]):
    def __init__(self, initial: T, unavailable: T, *, ttl: float, name: str) -> None:
        self._status = initial
        self._unavailable = unavailable
        self._ttl = ttl
        self._name = name
        self._lock = Lock()
        self._working = False
        self._expires = 0.0

    def current(self, probe: Callable[[], T]) -> T:
        with self._lock:
            if not self._working and monotonic() >= self._expires:
                self._working = True
                try:
                    Thread(target=self._refresh, args=(probe,), name=self._name, daemon=True).start()
                except RuntimeError:
                    self._working = False
                    self._expires = monotonic() + self._ttl
                    self._status = self._unavailable
            return self._status

    def _refresh(self, probe: Callable[[], T]) -> None:
        result = self._unavailable
        try:
            result = probe()
        except (OSError, RuntimeError):
            pass
        finally:
            with self._lock:
                self._status = result
                self._expires = monotonic() + self._ttl
                self._working = False
