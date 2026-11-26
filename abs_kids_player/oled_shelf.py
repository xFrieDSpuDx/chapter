"""Refresh the OLED's local download and listening-position view without holding up input."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TypeAlias

from .catalogue_settings import catalogue_request_key
from .config import AppConfig
from .download_store import account_downloads, books_with_downloads
from .library import sorted_books_for_menu
from .models import Book
from .progress_sync import progress_account, read_listening_progress
from .rotary_ui import ApplianceMenu


@dataclass
class LocalShelfState:
    downloaded: dict[str, Book]
    positions: dict[str, tuple[int, float]]


ShelfResult: TypeAlias = tuple[LocalShelfState, list[Book] | None]


def check_shelf(config: AppConfig, catalogue: list[Book], displayed: list[Book],
                previous: LocalShelfState | None) -> ShelfResult:
    copies = account_downloads(config)
    downloaded = {record.book.id: record.book for _, record in copies if record.complete}
    positions = read_listening_progress() if downloaded else {}
    account = progress_account(config)
    state = LocalShelfState(downloaded, {
        item: (edit.updated_at, edit.current_time)
        for item in downloaded if (edit := positions.get((account, item, ""))) is not None
    })
    if state == previous:
        return state, None
    # The menu may have a newer Stop or Finish edit than its last server catalogue.
    streamed = {book.id: book for book in displayed if not book.downloaded and not book.episode_id}
    base = [streamed.get(book.id, book) for book in catalogue]
    episodes = (book for book in displayed if book.episode_id)
    books = books_with_downloads(config, [*base, *episodes], copies=copies, positions=positions)
    ordered = sorted_books_for_menu(books, config.library_sort_mode)
    return state, ordered if ordered != displayed else None


class OledShelf:
    def __init__(self) -> None:
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oled-shelf")
        self._pending: Future[ShelfResult] | None = None
        self._catalogue: list[Book] | None = None
        self._displayed: list[Book] | None = None
        self._settings = ("", "")
        self._state: LocalShelfState | None = None

    def _matches(self, config: AppConfig, catalogue: list[Book], displayed: list[Book]) -> bool:
        # The menu and catalogue replace their lists on edits. Comparing identity also
        # catches a podcast refresh or web selection made while the disk check was running.
        return (catalogue is self._catalogue and displayed is self._displayed
                and self._settings == (catalogue_request_key(config), config.library_sort_mode))

    def request(self, config: AppConfig, catalogue: list[Book], menu: ApplianceMenu) -> None:
        if self._pending is not None:
            return
        if not self._matches(config, catalogue, menu.books):
            self._state = None
        self._catalogue, self._displayed = catalogue, menu.books
        self._settings = catalogue_request_key(config), config.library_sort_mode
        try:
            self._pending = self._worker.submit(check_shelf, config, catalogue, menu.books, self._state)
        except RuntimeError as error:
            # A failed thread start can leave a job in the executor's queue.
            self._worker.shutdown(wait=False, cancel_futures=True)
            self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oled-shelf")
            print(f"OLED shelf check could not start: {error}")

    def apply_ready(self, config: AppConfig, catalogue: list[Book], menu: ApplianceMenu) -> None:
        pending = self._pending
        if pending is None or not pending.done():
            return
        self._pending = None
        if not self._matches(config, catalogue, menu.books):
            self._state = None
            return
        try:
            self._state, books = pending.result()
        except Exception as error:
            print(f"OLED shelf check failed: {type(error).__name__}")
            return
        if menu.library_sort_mode != config.library_sort_mode:
            menu.set_library_sort_mode(config.library_sort_mode)
        if books is not None:
            menu.set_books(books)
            self._displayed = menu.books

    def close(self) -> None:
        self._worker.shutdown(wait=True, cancel_futures=True)
