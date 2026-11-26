"""Audiobookshelf library choices and the latest catalogue refresh status."""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from .api import AudiobookshelfError, client_from_config
from .config import AppConfig
from .library_cache import config_cache_key, library_cache_path
from .progress_sync import progress_account
from .storage import write_json_atomically


CATALOGUE_COMMAND = "catalogue_refresh"


@dataclass(frozen=True)
class LibraryOption:
    id: str
    name: str


@dataclass(frozen=True)
class CatalogueStatus:
    last_success_at: float = 0.0
    error: str = ""


class LibraryUnavailable(AudiobookshelfError):
    pass


def available_book_libraries(config: AppConfig) -> list[LibraryOption]:
    rows = client_from_config(config).get_libraries()
    options = {
        str(row["id"]): LibraryOption(str(row["id"]), str(row["name"]))
        for row in rows
        if row.get("mediaType") == "book" and row.get("id") and row.get("name")
    }
    return sorted(options.values(), key=lambda option: (option.name.casefold(), option.id))


def select_library(config: AppConfig, library_id: str) -> AppConfig:
    for option in available_book_libraries(config):
        if option.id == library_id:
            return apply_library_choice(config, option.id, option.name)
    raise LibraryUnavailable("Choose an accessible audiobook library.")


def apply_library_choice(config: AppConfig, library_id: str, library_name: str) -> AppConfig:
    return replace(
        config,
        library_id=library_id,
        library_name=library_name,
        pinned_shelf=config.pinned_shelf if config.library_id == library_id else "",
    )


def catalogue_status_path() -> Path:
    return library_cache_path().with_name("catalogue-status.json")


def catalogue_request_key(config: AppConfig) -> str:
    return f"{progress_account(config)}/{config_cache_key(config)}"


def catalogue_status_key(config: AppConfig) -> str:
    return f"{progress_account(config)}/{config.library_id}"


def load_catalogue_status(config: AppConfig) -> CatalogueStatus:
    if not config.is_ready:
        return CatalogueStatus()
    try:
        data = json.loads(catalogue_status_path().read_text(encoding="utf-8"))
        if data["key"] != catalogue_status_key(config):
            return CatalogueStatus()
        last_success = float(data["last_success_at"])
        if not math.isfinite(last_success) or last_success < 0:
            return CatalogueStatus()
        return CatalogueStatus(last_success, str(data["error"]))
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return CatalogueStatus()


def report_catalogue_result(config: AppConfig, error: str = "") -> None:
    if not config.is_ready:
        return
    status = CatalogueStatus(
        last_success_at=load_catalogue_status(config).last_success_at if error else time.time(), error=error
    )
    try:
        write_json_atomically(catalogue_status_path(), {"key": catalogue_status_key(config), **asdict(status)})
    except OSError as error:
        print(f"Could not save the library refresh result: {type(error).__name__}")


def format_last_refresh(status: CatalogueStatus) -> str:
    if status.last_success_at:
        try:
            return datetime.fromtimestamp(status.last_success_at).astimezone().strftime("%d %b %Y, %H:%M %Z")
        except (OSError, OverflowError, ValueError):
            pass
    return "No successful refresh recorded yet"
