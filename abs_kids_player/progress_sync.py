"""Keep the latest local listening position until Audiobookshelf accepts it."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .api import server_position
from .config import AppConfig
from .player_state import state_path
from .storage import write_json_atomically


@dataclass(frozen=True)
class PendingProgress:
    account: str
    item_id: str
    current_time: float
    duration: float
    updated_at: int
    episode_id: str = ""
    session_id: str = ""
    time_listened: float = 0.0
    synced: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return self.account, self.item_id, self.episode_id


@dataclass(eq=False)
class SessionProgress:
    """The server position this playback session started from or last sent."""

    checkpoint: tuple[float, int] | None
    offline_opened_at: int | None = None
    taken_over: tuple[float, int] | None = None

    def changed_on_server(self, live: tuple[float, int] | None) -> bool:
        if live is None or self.checkpoint is None:
            return False
        position, updated_at = live
        if self.offline_opened_at is not None and updated_at > self.offline_opened_at:
            # Both devices listened offline from one another. Compare their edit times.
            return False
        return updated_at > self.checkpoint[1] and not math.isclose(position, self.checkpoint[0], abs_tol=0.001, rel_tol=0)


@dataclass(frozen=True)
class ProgressSyncStatus:
    last_success_at: float = 0.0
    last_attempt_at: float = 0.0
    outcome: str = "not_recorded"
    item_id: str = ""
    episode_id: str = ""

    @property
    def item_key(self) -> tuple[str, str]:
        return self.item_id, self.episode_id


def progress_status_path() -> Path:
    return state_path().with_name("progress-sync-status.json")


def load_progress_sync_status(account: str) -> ProgressSyncStatus:
    if not account:
        return ProgressSyncStatus()
    try:
        data = json.loads(progress_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ProgressSyncStatus()
    if not isinstance(data, dict) or data.get("account") != account:
        return ProgressSyncStatus()
    last_success = data.get("last_success_at")
    last_attempt = data.get("last_attempt_at")
    outcome = data.get("outcome")
    if (
        type(last_success) not in (int, float) or not 0 <= last_success < 253_402_300_800
        or type(last_attempt) not in (int, float) or not 0 <= last_attempt < 253_402_300_800
        or outcome not in ("sent", "server_newer", "retrying")
    ):
        return ProgressSyncStatus()
    item_id, episode_id = data.get("item_id"), data.get("episode_id")
    return ProgressSyncStatus(last_success, last_attempt, outcome,
                              item_id if isinstance(item_id, str) else "",
                              episode_id if isinstance(episode_id, str) else "")


def _record_progress_sync_status(account: str, outcome: str, item_id: str = "", episode_id: str = "") -> None:
    now = time.time()
    successful_at = now if outcome == "sent" else load_progress_sync_status(account).last_success_at
    status = ProgressSyncStatus(successful_at, now, outcome, item_id, episode_id)
    try:
        write_json_atomically(progress_status_path(), {"account": account, **asdict(status)})
    except OSError as error:
        print(f"Could not save the progress sync status: {type(error).__name__}")


def progress_account(config: AppConfig) -> str:
    if not config.is_ready:
        return ""
    identity = config.username.strip().casefold() or config.token
    return hashlib.sha256(f"{config.server_url.rstrip('/')}\n{identity}".encode()).hexdigest()


def _read_progress_file(name: str) -> dict[tuple[str, str, str], PendingProgress]:
    try:
        rows = json.loads(state_path().with_name(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(rows, list):
        return {}
    updates = {}
    for row in rows:
        try:
            update = PendingProgress(**row)
            if not update.account or not update.item_id or not isinstance(update.updated_at, int):
                continue
            if not math.isfinite(update.current_time) or not math.isfinite(update.duration):
                continue
            if update.current_time < 0 or update.duration < 0:
                continue
            updates[update.key] = update
        except (TypeError, ValueError, OverflowError):
            continue
    return updates


def _load_positions(*, pending_only: bool) -> dict[tuple[str, str, str], PendingProgress]:
    pending = _read_progress_file("pending-progress.json")
    if pending_only and not pending:
        return {}
    saved = _read_progress_file("listening-positions.json")
    # After an interrupted acknowledgement the same edit can still be in both files.
    # Compare edit timestamps because the listener can intentionally rewind.
    outstanding = {
        key: edit for key, edit in pending.items()
        if key not in saved or edit.updated_at > saved[key].updated_at
    }
    return outstanding if pending_only else saved | outstanding


def read_pending_progress() -> dict[tuple[str, str, str], PendingProgress]:
    return _load_positions(pending_only=True)


def read_listening_progress() -> dict[tuple[str, str, str], PendingProgress]:
    return _load_positions(pending_only=False)


class ProgressSync:
    """Serialize writes across book changes and retry while playback is paused."""

    def __init__(self, *, retry_seconds: float = 15.0) -> None:
        self._positions = read_listening_progress()
        # Queue order decides who goes next. Each value is that item's earliest retry time.
        self._pending = OrderedDict(
            (key, 0.0) for key, position in self._positions.items() if not position.synced
        )
        self._lock = threading.Lock()
        self._active_account = ""
        self._sending = False
        self._worker_retry_at = 0.0
        self._retry_seconds = retry_seconds
        self._sent_positions: dict[tuple[str, str, str], float] = {}
        self._sessions: dict[tuple[str, str, str], SessionProgress] = {}

    def record(self, account: str, item_id: str, current_time: float, duration: float,
               episode_id: str = "", session_id: str = "", time_listened: float = 0,
               *, after: int = 0, session: SessionProgress | None = None) -> None:
        key = account, item_id, episode_id
        with self._lock:
            if session is not None:
                if session.taken_over is not None:
                    return
                self._sessions[key] = session
            else:
                self._sessions.pop(key, None)
            previous = self._positions.get(key)
            updated_at = max(time.time_ns() // 1_000_000, after + 1)
            if previous is not None:
                updated_at = max(updated_at, previous.updated_at + 1)
            self._positions[key] = PendingProgress(
                account, item_id, current_time, duration, updated_at, episode_id, session_id, time_listened
            )
            self._pending.setdefault(key, 0.0)
            self._save()

    def pump(self, config: AppConfig) -> None:
        account = progress_account(config)
        with self._lock:
            if account != self._active_account:
                self._active_account = account
                self._worker_retry_at = 0.0
            now = time.monotonic()
            if self._sending or now < self._worker_retry_at:
                return
            key = next(
                (key for key, retry_at in self._pending.items() if key[0] == account and retry_at <= now),
                None,
            )
            if key is None:
                return
            update = self._positions[key]
            try:
                threading.Thread(target=self._send, args=(config, update), daemon=True).start()
            except (OSError, RuntimeError) as error:
                self._worker_retry_at = time.monotonic() + self._retry_seconds
                _record_progress_sync_status(account, "retrying", update.item_id, update.episode_id)
                print(f"Could not start progress worker: {type(error).__name__}")
            else:
                self._sending = True

    def _send(self, config: AppConfig, update: PendingProgress) -> None:
        from .api import client_from_config

        completed = False
        sent = False
        superseded = False
        resolved = update
        session = None
        live = None
        try:
            client = client_from_config(config)
            remote = client.get_progress(update.item_id, episode_id=update.episode_id) or {}
            with self._lock:
                if self._active_account != update.account or self._positions.get(update.key) != update:
                    superseded = True
                    return
                previous_position = self._sent_positions.get(update.key)
                session = self._sessions.get(update.key)
            live = server_position(remote)
            if remote and live is None:
                raise ValueError("Invalid Audiobookshelf progress")
            matches_our_last_write = previous_position is not None and remote.get("currentTime") == previous_position
            changed_elsewhere = session is not None and session.changed_on_server(live)
            server_is_newer = live is not None and live[1] > update.updated_at
            if matches_our_last_write or not (changed_elsewhere or server_is_newer):
                client.update_progress(
                    update.item_id, update.current_time, update.duration,
                    episode_id=update.episode_id, session_id=update.session_id, time_listened=update.time_listened,
                )
                sent = True
            else:
                resolved = replace(update, current_time=live[0], updated_at=live[1])
            completed = True
        except (RuntimeError, OSError, ValueError, TypeError) as error:
            print(f"Progress sync will retry: {type(error).__name__}")
        finally:
            with self._lock:
                if sent:
                    self._sent_positions[update.key] = update.current_time
                    if session is not None:
                        session.checkpoint = update.current_time, live[1] if live else 0
                        session.offline_opened_at = None
                elif completed and session is not None:
                    session.taken_over = live
                same_session_yielded = (session is not None and session.taken_over is not None
                                        and self._sessions.get(update.key) is session)
                if completed and (self._positions.get(update.key) == update or same_session_yielded):
                    if same_session_yielded:
                        # Retire even an in-flight local edit if power is lost before the queue is cleared.
                        resolved = replace(resolved, updated_at=max(resolved.updated_at, self._positions[update.key].updated_at))
                    self._positions[update.key] = replace(resolved, synced=True)
                    self._sent_positions.pop(update.key, None)
                    self._sessions.pop(update.key, None)
                    del self._pending[update.key]
                    self._save(update_saved_positions=True)
                else:
                    if not completed and not superseded:
                        self._pending[update.key] = time.monotonic() + self._retry_seconds
                    self._pending.move_to_end(update.key)
                if self._active_account == update.account and not superseded:
                    outcome = "sent" if sent else "server_newer" if completed else "retrying"
                    _record_progress_sync_status(update.account, outcome, update.item_id, update.episode_id)
                self._sending = False

    def _save(self, *, update_saved_positions: bool = False) -> None:
        try:
            saved, pending = [], []
            for update in self._positions.values():
                row = asdict(update)
                if update.synced:
                    saved.append(row)
                else:
                    del row["synced"]
                    pending.append(row)
            if update_saved_positions:
                write_json_atomically(state_path().with_name("listening-positions.json"), saved, durable=True)
            write_json_atomically(state_path().with_name("pending-progress.json"), pending, durable=True)
        except OSError as error:
            print(f"Could not save pending progress: {type(error).__name__}")
