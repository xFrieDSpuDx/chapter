from __future__ import annotations

import fcntl
import json
import math
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .storage import ensure_storage_directory, write_json_atomically


STATE_DIR = Path.home() / ".local" / "state" / "abs-kids-player"
STATUS_PATH = STATE_DIR / "web-player-status.json"
COMMANDS_PATH = STATE_DIR / "web-player-commands.json"


@dataclass(frozen=True)
class WebPlayerStatus:
    title: str = ""
    author: str = ""
    current_time: float = 0
    duration: float = 0
    is_playing: bool = False
    volume_percent: int = 50
    muted: bool = False
    updated_at: float = 0
    audio_output: str = ""
    display_drawn_at: float = 0
    cover_item_id: str = ""
    session_key: str = ""
    account: str = ""
    episode_id: str = ""
    downloaded: bool = False
    playback_rate: float = 1.0
    library_id: str = ""
    opening_key: str = ""

    @property
    def has_session(self) -> bool:
        return bool(self.title.strip())


@dataclass(frozen=True)
class WebPlayerCommand:
    action: str
    value: int | None = None
    id: str = ""
    created_at: float = 0
    catalogue_key: str = ""
    library_id: str = ""
    book_id: str = ""
    resume_time: float = 0
    start_over: bool = False
    session_key: str = ""
    series: str = ""
    local_only: bool = False


class WebCommandInbox:
    """Poll the command queue without holding up the knobs when storage is unavailable."""

    def __init__(self, consume: Callable[[], list[WebPlayerCommand]]) -> None:
        self._consume = consume
        self._retry_at = 0.0

    def read(self) -> list[WebPlayerCommand]:
        now = time.monotonic()
        if now < self._retry_at:
            return []
        try:
            return self._consume()
        except OSError as error:
            print(f"Could not read queued web commands: {error}")
            self._retry_at = now + 5
            return []


def save_web_player_status(status: WebPlayerStatus) -> None:
    data = asdict(status)
    data["updated_at"] = time.time()
    write_json_atomically(status_path(), data)


def load_web_player_status() -> WebPlayerStatus:
    try:
        data = json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return WebPlayerStatus()
    if not isinstance(data, dict):
        return WebPlayerStatus()

    output = data.get("audio_output")
    return WebPlayerStatus(
        title=str(data.get("title", "")),
        author=str(data.get("author", "")),
        current_time=_finite_number(data.get("current_time")),
        duration=_finite_number(data.get("duration")),
        is_playing=bool(data.get("is_playing")),
        volume_percent=clamp_int(data.get("volume_percent"), 0, 100, 50),
        muted=bool(data.get("muted")),
        updated_at=_finite_number(data.get("updated_at")),
        audio_output=output if output in ("speaker", "bluetooth") else "",
        display_drawn_at=_finite_number(data.get("display_drawn_at")),
        cover_item_id=str(data.get("cover_item_id") or ""),
        session_key=str(data.get("session_key") or ""),
        opening_key=str(data.get("opening_key") or ""),
        account=str(data.get("account") or ""),
        episode_id=str(data.get("episode_id") or ""),
        downloaded=data.get("downloaded") is True,
        playback_rate=_finite_number(data.get("playback_rate")) or 1.0,
        library_id=str(data.get("library_id") or ""),
    )


def queue_web_player_command(
    action: str,
    value: int | None = None,
    *,
    catalogue_key: str = "",
    library_id: str = "",
    book_id: str = "",
    resume_time: float = 0,
    start_over: bool = False,
    session_key: str = "",
    series: str = "",
    local_only: bool = False,
) -> WebPlayerCommand:
    command = WebPlayerCommand(
        action=action,
        value=value,
        id=uuid.uuid4().hex,
        created_at=time.time(),
        catalogue_key=catalogue_key,
        library_id=library_id,
        book_id=book_id,
        resume_time=max(0, _finite_number(resume_time)),
        start_over=start_over,
        session_key=session_key,
        series=series,
        local_only=local_only,
    )
    path = commands_path()
    with lock_state_directory(path):
        commands = _read_command_data(path)
        commands.append(asdict(command))
        write_json_atomically(path, commands)
    return command


def consume_web_player_commands() -> list[WebPlayerCommand]:
    path = commands_path()
    with lock_state_directory(path):
        entries = _read_command_data(path)
        if entries:
            write_json_atomically(path, [])
    return [
        WebPlayerCommand(
            action=item["action"],
            value=item["value"] if isinstance(item.get("value"), int) else None,
            id=str(item.get("id", "")),
            created_at=_finite_number(item.get("created_at")),
            catalogue_key=str(item.get("catalogue_key") or ""),
            library_id=str(item.get("library_id") or ""),
            book_id=str(item.get("book_id") or ""),
            resume_time=max(0, _finite_number(item.get("resume_time"))),
            start_over=item.get("start_over") is True,
            session_key=str(item.get("session_key") or ""),
            series=str(item.get("series") or ""),
            local_only=item.get("local_only") is True,
        )
        for item in entries
        if isinstance(item, dict) and isinstance(item.get("action"), str) and item["action"]
    ]


def command_result(command_id: str) -> str:
    return _read_command_results().get(command_id, "queued")


def save_command_result(command: WebPlayerCommand, outcome: str) -> None:
    if not command.id:
        return
    path = command_results_path()
    try:
        with lock_state_directory(path):
            results = _read_command_results()
            results[command.id] = outcome
            write_json_atomically(path, dict(list(results.items())[-64:]))
    except OSError as error:
        print(f"Could not save command result: {type(error).__name__}")


def _read_command_results() -> dict[str, str]:
    try:
        data = json.loads(command_results_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def command_results_path() -> Path:
    return commands_path().with_name("web-player-command-results.json")


@contextmanager
def lock_state_directory(path: Path) -> Iterator[None]:
    # Replacing the queue changes its inode. The directory stays the same for both services.
    if not path.parent.is_dir():
        ensure_storage_directory(path.parent)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _read_command_data(path: Path) -> list[Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _finite_number(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if math.isfinite(number) else 0


def status_path() -> Path:
    configured_path = os.environ.get("ABS_KIDS_PLAYER_WEB_STATUS_PATH", "").strip()
    if configured_path:
        return Path(configured_path).expanduser()
    configured_dir = os.environ.get("ABS_KIDS_PLAYER_STATE_DIR", "").strip()
    if configured_dir:
        return Path(configured_dir).expanduser() / STATUS_PATH.name
    return STATUS_PATH


def commands_path() -> Path:
    configured_path = os.environ.get("ABS_KIDS_PLAYER_WEB_COMMANDS_PATH", "").strip()
    if configured_path:
        return Path(configured_path).expanduser()
    configured_dir = os.environ.get("ABS_KIDS_PLAYER_STATE_DIR", "").strip()
    if configured_dir:
        return Path(configured_dir).expanduser() / COMMANDS_PATH.name
    return COMMANDS_PATH


def clamp_int(value: object, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return min(max(parsed, minimum), maximum)
