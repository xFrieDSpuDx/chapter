"""Sample available space without blocking an HTTP request."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock, Thread
from time import monotonic

from .config import AppConfig
from .download_transfer import DISK_RESERVE_BYTES, MIB, storage_usage


SPACE_REFRESH_SECONDS = 2.0


@dataclass(frozen=True)
class SpaceReading:
    remaining: int | None
    updating: bool


def available_for_downloads(config: AppConfig, used: int, free: int) -> int:
    return max(0, min(config.download_limit_mib * MIB - used, free - DISK_RESERVE_BYTES))


class DownloadSpace:
    def __init__(self, *, sample: Callable[[], tuple[int, int]] | None = None, clock=monotonic) -> None:
        self._lock = Lock()
        self._sample_usage = sample or storage_usage
        self._clock = clock
        self._sample: tuple[int, int] | None = None
        self._next_check = 0.0
        self._working = False
        self._revision = 0
        self._invalidated = False

    def invalidate(self) -> None:
        with self._lock:
            self._revision += 1
            self._invalidated = True
            self._next_check = 0.0
            self._start_sample()

    def reading(self, config: AppConfig) -> SpaceReading:
        with self._lock:
            self._start_sample()
            remaining = available_for_downloads(config, *self._sample) if self._sample is not None else None
            return SpaceReading(remaining, self._invalidated or (remaining is None and self._working))

    def _start_sample(self) -> None:
        if not self._working and self._clock() >= self._next_check:
            self._working = True
            Thread(target=self._refresh, args=(self._revision,), name="chapter-download-space", daemon=True).start()

    def _refresh(self, revision: int) -> None:
        try:
            sample = self._sample_usage()
        except OSError:
            sample = None
        with self._lock:
            self._working = False
            if revision != self._revision:
                self._start_sample()
                return
            if sample is not None:
                self._sample = sample
            self._invalidated = False
            self._next_check = self._clock() + SPACE_REFRESH_SECONDS
