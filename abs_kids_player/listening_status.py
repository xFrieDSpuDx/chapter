"""Share session details and describe the saved position for the active book."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from .bedtime import BedtimeTimer
from .config import AppConfig
from .player_control import WebPlayerStatus, status_path
from .progress_sync import load_progress_sync_status, progress_account, read_listening_progress
from .storage import write_json_atomically


if TYPE_CHECKING:
    from .playback_owner import PlaybackSnapshot


def details_path() -> Path:
    return status_path().with_name("web-player-listening.json")


class ListeningDetailsPublisher:
    def __init__(self) -> None:
        self._published = None
        self._session_key = ""
        self._chapters = []
        self._retry_at = 0.0

    def publish(self, state: PlaybackSnapshot, undo_position: float | None, bedtime: BedtimeTimer, series: str = "") -> None:
        if not state.session_key:
            self._published = None
            self._session_key = ""
            self._chapters = []
            return
        if state.session_key != self._session_key:
            self._session_key = state.session_key
            self._chapters = [asdict(chapter) for chapter in state.session.chapters] if state.session else []
        can_undo = undo_position is not None
        detail = (state.session_key, can_undo, bedtime.deadline, bedtime.chapter_end, bedtime.preset_minutes, bedtime.sleeping, series)
        now = time.monotonic()
        if detail == self._published or now < self._retry_at:
            return
        data = {
            "sessionKey": state.session_key, "chapters": self._chapters, "canUndo": can_undo,
            "sleepUntil": time.time() + (bedtime.deadline - now) if bedtime.deadline is not None else None,
            "sleepChapterEnd": bedtime.chapter_end, "sleepPreset": bedtime.preset_minutes if not bedtime.sleeping else 0,
            "sleeping": bedtime.sleeping,
            "series": series,
        }
        try:
            write_json_atomically(details_path(), data, compact=True)
        except OSError as error:
            print(f"Could not publish listening controls: {type(error).__name__}")
            self._retry_at = now + 5
        else:
            self._published = detail


def listening_details(status: WebPlayerStatus) -> dict:
    if not status.session_key:
        return {}
    try:
        data = json.loads(details_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("sessionKey") == status.session_key else {}


def listening_progress(config: AppConfig, status: WebPlayerStatus) -> dict:
    account = progress_account(config)
    if not status.has_session or not account or account != status.account:
        return {}
    key = account, status.cover_item_id, status.episode_id
    saved = read_listening_progress().get(key)
    if saved is None:
        return {"state": "not_saved", "label": "Listening position has yet to be saved."}
    attempt = load_progress_sync_status(account)
    state = "waiting"
    if saved.synced:
        state = "synced"
    elif attempt.outcome == "retrying" and attempt.item_key == key[1:]:
        state = "retrying"
    labels = {
        "synced": "Synced to Audiobookshelf",
        "waiting": "Saved on Chapter · Waiting to sync",
        "retrying": "Saved on Chapter · Sync will retry",
    }
    return {
        "state": state,
        "label": labels[state],
        "position": saved.current_time,
        "savedAt": saved.updated_at / 1000,
    }


def listening_source(status: WebPlayerStatus) -> str:
    if not status.has_session:
        return ""
    return "Downloaded copy" if status.downloaded else "Streaming"


def matches_listening_account(config: AppConfig, status: WebPlayerStatus) -> bool:
    return status.account == progress_account(config) or bool(status.episode_id and status.account == "local-rss")
