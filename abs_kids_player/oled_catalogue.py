"""Fetch the OLED catalogue and publish the result of a library refresh."""

from __future__ import annotations

import queue
import threading
import traceback
from dataclasses import dataclass

from .api import AudiobookshelfClient, AudiobookshelfError
from .catalogue_settings import (
    LibraryOption,
    LibraryUnavailable,
    apply_library_choice,
    catalogue_request_key,
    report_catalogue_result,
    select_library,
)
from .config import AppConfig, ConfigReadError, load_config, update_config
from .connection_health import ConnectionState, connection_error_state
from .display_frames import TwoLineFrame
from .library import load_library_books
from .library_cache import save_cached_books
from .models import Book
from .network import setup_address_frame
from .player_control import WebPlayerCommand, save_command_result


@dataclass
class BookLoadResult:
    books: list[Book] | None = None
    error_frame: TwoLineFrame | None = None
    generation: int = 0
    library: LibraryOption | None = None


def book_load_result(
    config: AppConfig,
    client_factory=AudiobookshelfClient,
    lan_ip: str | None = None,
) -> BookLoadResult:
    if not config.is_ready:
        return BookLoadResult(error_frame=setup_address_frame(lan_ip))

    try:
        return BookLoadResult(books=load_library_books(config, client_factory))
    except (AudiobookshelfError, OSError, ValueError) as error:
        return BookLoadResult(error_frame=book_load_error_frame(error))


def book_load_error_frame(error: Exception) -> TwoLineFrame:
    state = (ConnectionState.LIBRARY_UNAVAILABLE if isinstance(error, LibraryUnavailable)
             else connection_error_state(error, checking_library=True))
    print(f"Book refresh failed: {state.value}")
    label = {
        ConnectionState.UNREACHABLE: "Server unreachable",
        ConnectionState.TIMED_OUT: "Server timed out",
        ConnectionState.SIGN_IN_REQUIRED: "Sign-in required",
        ConnectionState.LIBRARY_UNAVAILABLE: "Library unavailable",
    }.get(state, "Books unavailable")
    return TwoLineFrame(label, "Press to retry")


def start_book_load_worker(
    config: AppConfig,
    results: queue.SimpleQueue[BookLoadResult],
    client_factory=AudiobookshelfClient,
    generation: int = 0,
    library_id: str = "",
) -> None:
    def run() -> None:
        try:
            target = select_library(config, library_id) if library_id else config
            result = book_load_result(target, client_factory)
            if library_id and result.books is not None:
                result.library = LibraryOption(target.library_id, target.library_name)
        except (AudiobookshelfError, OSError, ValueError) as error:
            result = BookLoadResult(error_frame=book_load_error_frame(error))
        except Exception:
            print("Book refresh worker failed:")
            traceback.print_exc()
            result = BookLoadResult(error_frame=TwoLineFrame("Books unavailable", "Press to retry"))
        result.generation = generation
        results.put(result)

    try:
        threading.Thread(target=run, name="chapter-oled-catalogue", daemon=True).start()
    except RuntimeError:
        results.put(BookLoadResult(
            generation=generation, error_frame=TwoLineFrame("Books unavailable", "Press to retry"),
        ))


def finish_book_load(
    result: BookLoadResult, config: AppConfig, request: WebPlayerCommand | None,
) -> AppConfig:
    if result.books is not None and result.library is not None:
        def select(current: AppConfig) -> AppConfig:
            if catalogue_request_key(current) != catalogue_request_key(config):
                raise ValueError("Library changed during refresh")
            return apply_library_choice(current, result.library.id, result.library.name)

        try:
            _, updated = update_config(select)
        except ValueError:
            result.books = None
            result.error_frame = TwoLineFrame("Library changed", "Press to retry")
        except OSError:
            result.books = None
            result.error_frame = TwoLineFrame("Could not save library", "Press to retry")
        else:
            config = updated

    outcome = "rejected"
    if result.books is None:
        error = result.error_frame.top if result.error_frame else "Books unavailable"
        report_catalogue_result(config, error)
    elif save_cached_books(config, result.books):
        report_catalogue_result(config)
        outcome = "applied"
    else:
        # The OLED can still use the fetched books for this session.
        report_catalogue_result(config, "Could not save the catalogue on the player")
    if request is not None:
        save_command_result(request, outcome)
    return config


class OledCatalogue:
    """Coordinate scheduled and requested refreshes on the OLED loop thread."""

    def __init__(self, refresh_seconds: float) -> None:
        self._interval = refresh_seconds
        self._next_refresh = 0.0
        self._generation = 0
        self._config_key = ""
        self._request: WebPlayerCommand | None = None
        self._results: queue.SimpleQueue[BookLoadResult] = queue.SimpleQueue()
        self.in_progress = False

    def refresh_when_online(self) -> None:
        self._next_refresh = 0.0

    def invalidate(self) -> None:
        if self._request:
            save_command_result(self._request, "superseded")
            self._request = None
        self._generation += 1
        self.in_progress = False
        self.refresh_when_online()

    def start_if_due(self, now: float) -> AppConfig | None:
        if self.in_progress or now < self._next_refresh:
            return None
        config = load_config()
        if not config.is_ready:
            self._next_refresh = now + self._interval
            return None
        self._start(config, now)
        return config

    def request_latest(self, commands: list[WebPlayerCommand], config: AppConfig, now: float) -> None:
        key = catalogue_request_key(config)
        latest = None
        for command in commands:
            if not config.is_ready or command.catalogue_key != key:
                save_command_result(command, "superseded")
                continue
            if latest:
                save_command_result(latest, "superseded")
            latest = command
        if latest is None:
            return
        previous = self._request
        if previous:
            save_command_result(previous, "superseded")
        self._request = latest
        if self.in_progress and key == self._config_key and previous and previous.library_id == latest.library_id:
            return  # The same lookup can satisfy the newer request.
        self._start(config, now)

    def _start(self, config: AppConfig, now: float) -> None:
        self._generation += 1
        self._config_key = catalogue_request_key(config)
        self.in_progress = True
        self._next_refresh = now + self._interval
        start_book_load_worker(config, self._results, generation=self._generation,
                               library_id=self._request.library_id if self._request else "")

    def poll(self) -> tuple[BookLoadResult, AppConfig] | None:
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                return None
            if self.in_progress and result.generation == self._generation:
                break
        try:
            current = load_config()
        except ConfigReadError:
            self._results.put(result)  # Retry the same result after the settings are readable.
            raise
        if self._config_key != catalogue_request_key(current):
            self.invalidate()
            return None
        effective = finish_book_load(result, current, self._request)
        self._request = None
        self.in_progress = False
        return result, effective
