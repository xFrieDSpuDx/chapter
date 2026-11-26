"""Run the persistent download queue on the setup server."""

from __future__ import annotations

import errno
import logging
import shutil
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from http.client import HTTPException, IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError

from .api import AudiobookshelfError
from .config import AppConfig, load_config
from .download_assets import DownloadAssetBackfill
from .download_file_usage import download_file_usage
from .download_queue_store import (
    CANCELLED,
    DOWNLOADED,
    DOWNLOADING,
    FAILED,
    RUNNABLE_STATES,
    WAITING,
    DownloadRequest,
    read_download_queue,
    save_download_queue,
)
from .download_store import (
    DownloadedBook,
    account_directory,
    book_directory,
    invalidate_web_downloads,
    lock_download,
    read_download,
)
from .download_transfer import DownloadCancelled, DownloadError, DownloadInterrupted, transfer_book
from .models import Book
from .storage import ensure_storage_directory


LOGGER = logging.getLogger(__name__)
RETRY_SECONDS = 10
IDLE_CHECK_SECONDS = 2


@dataclass(frozen=True)
class ActiveDownload:
    account: Path
    directory: Path
    request: DownloadRequest
    stop: threading.Event


@dataclass(frozen=True)
class DownloadActivity:
    downloading: int = 0
    in_queue: int = 0


def retryable_download_error(error: Exception) -> bool:
    cause = error.__cause__ if isinstance(error, AudiobookshelfError) else error
    if isinstance(cause, HTTPError):
        if cause.fp is not None:
            cause.close()
        return cause.code in (408, 429) or 500 <= cause.code < 600
    if isinstance(cause, OSError) and cause.errno in (errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH):
        return True
    return isinstance(cause, (DownloadInterrupted, URLError, ConnectionError, TimeoutError, IncompleteRead))


def download_failure_message(error: Exception) -> str:
    if isinstance(error, DownloadError):
        return str(error)
    cause = error.__cause__ if isinstance(error, AudiobookshelfError) else error
    if isinstance(cause, HTTPError) and cause.code in (401, 403):
        return "Audiobookshelf refused the download. Check the account permissions."
    if isinstance(cause, HTTPError) and cause.code == 404:
        return "Audiobookshelf could not find the audio file."
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return "There is insufficient free space on the microSD card."
    return "The download could not finish. Check Audiobookshelf and the device storage."


class Downloads:
    def __init__(self, *, config_loader: Callable[[], AppConfig] = load_config,
                 clock: Callable[[], float] = time.monotonic, retry_seconds: float = RETRY_SECONDS) -> None:
        self._config_loader = config_loader
        self._clock = clock
        self._retry_seconds = retry_seconds
        self._lock = threading.Lock()
        self._writes = threading.Lock()
        self._assets = DownloadAssetBackfill()
        self._config_revision = 0
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._worker: threading.Thread | None = None
        self._requests: dict[Path, dict[str, DownloadRequest]] = {}
        self._retry_after: dict[Path, float] = {}
        self._active: ActiveDownload | None = None

    def start_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._shutdown.clear()
            self._worker = threading.Thread(target=self._run_worker, name="chapter-download", daemon=True)
            self._worker.start()

    def close(self) -> None:
        self._shutdown.set()
        with self._lock:
            if self._active:
                self._active.stop.set()
        self._wake.set()
        if self._worker:
            self._worker.join(timeout=1)

    def configuration_changed(self, config: AppConfig) -> None:
        with self._lock:
            self._config_revision += 1
            if self._active and self._active.account != account_directory(config):
                self._active.stop.set()
        self._wake.set()

    def start(self, config: AppConfig, book: Book) -> None:
        account = account_directory(config)
        directory = book_directory(config, book.id)
        with self._writes:
            requests = self._queue(account)
            current = requests.get(book.id)
            if current and current.state in RUNNABLE_STATES:
                return
            if (manifest := read_download(directory)) and manifest.complete:
                return
            updated = dict(requests)
            updated.pop(book.id, None)
            updated[book.id] = DownloadRequest(replace(book, cover_url="", downloaded=False))
            self._save_queue(account, updated)
            with self._lock:
                self._retry_after.pop(directory, None)
        self._wake.set()

    def cancel(self, config: AppConfig, item_id: str) -> None:
        account = account_directory(config)
        directory = book_directory(config, item_id)
        with self._writes:
            requests = self._queue(account)
            request = requests.get(item_id)
            if request is None or request.state not in RUNNABLE_STATES:
                return
            self._save_queue(account, {
                **requests, item_id: replace(request, state=CANCELLED, message="Retry to continue."),
            })
            with self._lock:
                self._retry_after.pop(directory, None)
                if self._active and self._active.directory == directory:
                    self._active.stop.set()
        self._wake.set()

    def remove(self, config: AppConfig, item_id: str) -> None:
        account = account_directory(config)
        directory = book_directory(config, item_id)
        with self._writes:
            requests = self._queue(account)
            request = requests.get(item_id)
            with self._lock:
                running = self._active and self._active.directory == directory
            if (request and request.state in RUNNABLE_STATES) or running:
                raise DownloadError("Cancel the download and wait for it to stop before removing it.")
            if directory.is_dir():
                try:
                    with lock_download(directory, shared=False):
                        shutil.rmtree(directory)
                    invalidate_web_downloads()
                except BlockingIOError as error:
                    raise DownloadError("This download is open in the player. Open another book first.") from error
            if request:
                self._save_queue(account, {key: row for key, row in requests.items() if key != item_id})
            with self._lock:
                self._retry_after.pop(directory, None)

    def queued_books(self, config: AppConfig) -> list[Book]:
        if not config.is_ready:
            return []
        return [request.book for request in self._queue(account_directory(config)).values()]

    def activity(self, config: AppConfig, completed: Collection[str]) -> DownloadActivity:
        if not config.is_ready:
            return DownloadActivity()
        account = account_directory(config)
        self._queue(account)
        with self._lock:
            pending = {
                item_id for item_id, request in self._requests[account].items()
                if request.state in RUNNABLE_STATES and item_id not in completed
            }
            active = self._active
            downloading = (
                active is not None and active.account == account and not active.stop.is_set()
                and active.request.book.id in pending
            )
        return DownloadActivity(int(downloading), len(pending))

    def status(self, config: AppConfig, item_id: str) -> tuple[str, int, int]:
        directory = book_directory(config, item_id)
        return self.status_from_manifest(config, item_id, read_download(directory))

    def status_from_manifest(
        self, config: AppConfig, item_id: str, manifest: DownloadedBook | None,
    ) -> tuple[str, int, int]:
        if manifest and manifest.complete:
            size = manifest.total_bytes
            return DOWNLOADED, size, size
        directory = book_directory(config, item_id)
        request = self._queue(directory.parent).get(item_id)
        with self._lock:
            active = self._active
            running = active and active.directory == directory and not active.stop.is_set()
        size = download_file_usage(directory).audio_bytes if manifest else 0
        if running:
            status = DOWNLOADING
        elif request:
            status = f"{request.state}. {request.message}" if request.message else request.state
        else:
            status = (manifest.error or "Interrupted. Retry to continue.") if manifest else ""
        total = manifest.total_bytes if manifest else 0
        return status, min(size, total), total

    def process_next(self) -> bool:
        with self._lock:
            revision = self._config_revision
        config = self._config_loader()
        if not config.is_ready:
            return False
        account = account_directory(config)
        self._queue(account)
        with self._lock:
            if self._active or self._shutdown.is_set() or revision != self._config_revision:
                return False
            active = self._next_request(config, account)
            if active is None:
                return False
            self._active = active

        error = None
        completed = False
        try:
            transfer_book(config, active.request.book, active.directory, active.stop)
            completed = True
        except (
            DownloadError, AudiobookshelfError, URLError, HTTPException, OSError,
            ValueError, TypeError, KeyError, AttributeError,
        ) as failure:
            error = failure
        finally:
            with self._writes:
                try:
                    if error is not None or completed:
                        self._record_result(active, error)
                finally:
                    with self._lock:
                        self._active = None
        return True

    def _queue(self, account: Path) -> dict[str, DownloadRequest]:
        with self._lock:
            current = self._requests.get(account)
            if current is not None:
                return current
        loaded = read_download_queue(account)
        with self._lock:
            return self._requests.setdefault(account, loaded)

    def _save_queue(self, account: Path, requests: dict[str, DownloadRequest]) -> None:
        ensure_storage_directory(account.parent)
        save_download_queue(account, requests)
        with self._lock:
            self._requests[account] = requests

    def _next_request(self, config: AppConfig, account: Path) -> ActiveDownload | None:
        now = self._clock()
        for request in self._requests[account].values():
            if request.state in RUNNABLE_STATES:
                directory = book_directory(config, request.book.id)
                if self._retry_after.get(directory, 0) <= now:
                    return ActiveDownload(account, directory, request, threading.Event())
        return None

    def _record_result(self, active: ActiveDownload, error: Exception | None) -> None:
        item_id = active.request.book.id
        requests = self._queue(active.account)
        if requests.get(item_id) is not active.request or active.stop.is_set() or isinstance(error, DownloadCancelled):
            return
        updated = dict(requests)
        if error is None:
            updated.pop(item_id)
        elif retryable_download_error(error):
            updated[item_id] = replace(active.request, state=WAITING)
            with self._lock:
                self._retry_after[active.directory] = self._clock() + self._retry_seconds
        else:
            updated[item_id] = replace(active.request, state=FAILED, message=download_failure_message(error))
        if updated != requests:
            self._save_queue(active.account, updated)
        if error is None or updated[item_id].state == FAILED:
            with self._lock:
                self._retry_after.pop(active.directory, None)

    def _run_worker(self) -> None:
        while not self._shutdown.is_set():
            self._wake.clear()
            try:
                if self.process_next():
                    continue
                if not self._shutdown.is_set():
                    self._assets.schedule(self._config_loader())
            except OSError:
                LOGGER.warning("Could not read or save the download queue. Will retry.")
            self._wake.wait(IDLE_CHECK_SECONDS)


downloads = Downloads()
