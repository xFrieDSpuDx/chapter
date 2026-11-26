"""Persistent, catalogue-scoped listening order shared by the browser and OLED."""

import json
from dataclasses import dataclass
from pathlib import Path

from .catalogue_settings import catalogue_status_key
from .config import AppConfig
from .library import next_series_book
from .models import Book, PlaybackSession
from .player_control import lock_state_directory
from .player_state import state_path
from .progress_sync import progress_account
from .storage import write_json_atomically


MAX_QUEUE_LENGTH = 100


@dataclass(frozen=True)
class ListeningQueue:
    items: tuple[str, ...] = ()
    auto_advance: bool = False


def queue_path() -> Path:
    return state_path().with_name("listening-queue.json")


def _queues() -> dict:
    try:
        data = json.loads(queue_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_queue(data: dict, catalogue: str) -> ListeningQueue:
    row = data.get(catalogue)
    if not isinstance(row, dict) or not isinstance(row.get("items"), list):
        return ListeningQueue()
    items = tuple(dict.fromkeys(item for item in row["items"] if isinstance(item, str) and item))
    return ListeningQueue(items[:MAX_QUEUE_LENGTH], row.get("auto_advance") is True)


def read_queue(catalogue: str) -> ListeningQueue:
    return _read_queue(_queues(), catalogue)


def edit_queue(catalogue: str, action: str, item: str = "") -> ListeningQueue:
    path = queue_path()
    with lock_state_directory(path):
        data = _queues()
        previous = _read_queue(data, catalogue)
        items = list(previous.items)
        auto = previous.auto_advance
        if action == "add" and item not in items:
            if not item or len(items) >= MAX_QUEUE_LENGTH:
                raise ValueError("The listening queue is full.")
            items.append(item)
        elif action == "remove" and item in items:
            items.remove(item)
        elif action in ("up", "down") and item in items:
            index = items.index(item)
            target = index + (-1 if action == "up" else 1)
            if 0 <= target < len(items):
                items[index], items[target] = items[target], items[index]
        elif action == "auto":
            auto = item == "1"
        result = ListeningQueue(tuple(items), auto)
        if result != previous:
            data[catalogue] = {"items": items, "auto_advance": auto}
            write_json_atomically(path, data, durable=True)
        return result


def next_queued_book(
    queue: ListeningQueue, books: list[Book], finished_item: str, series: str = "",
) -> Book | None:
    if not queue.auto_advance:
        return None
    available = {book.id: book for book in books if not book.episode_id}
    for item in queue.items:
        if item != finished_item and item in available:
            return available[item]
    return next_series_book(list(available.values()), finished_item, series)


def remove_started_book(config: AppConfig, session: PlaybackSession) -> None:
    if (session.episode_id or not session.progress_account or session.progress_account != progress_account(config)
            or session.library_id != config.library_id):
        return
    key = catalogue_status_key(config)
    if session.library_item_id in read_queue(key).items:
        try:
            edit_queue(key, "remove", session.library_item_id)
        except OSError as error:
            print(f"Could not remove the playing book from the queue: {type(error).__name__}")
