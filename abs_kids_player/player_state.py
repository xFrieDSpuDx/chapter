from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .audio import VolumeState
from .storage import write_json_atomically


STATE_DIR = Path.home() / ".local" / "state" / "abs-kids-player"
STATE_PATH = STATE_DIR / "player-state.json"


@dataclass(frozen=True)
class LastPlaybackState:
    book_id: str
    title: str
    current_time: float = 0
    updated_at: float = 0
    episode_id: str = ""
    account: str = ""

    @property
    def is_ready(self) -> bool:
        return bool(self.book_id.strip() and self.title.strip())


def load_player_state() -> LastPlaybackState | None:
    path = state_path()
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    state = LastPlaybackState(
        book_id=str(data.get("book_id", "")),
        title=str(data.get("title", "")),
        current_time=float(data.get("current_time") or 0),
        updated_at=float(data.get("updated_at") or 0),
        episode_id=str(data.get("episode_id") or ""),
        account=str(data.get("account") or ""),
    )
    return state if state.is_ready else None


def save_last_playback(
    book_id: str,
    title: str,
    current_time: float,
    now: float | None = None,
    *,
    episode_id: str = "",
    account: str = "",
) -> None:
    state = LastPlaybackState(
        book_id=book_id,
        title=title,
        current_time=max(0.0, current_time),
        updated_at=time.time() if now is None else now,
        episode_id=episode_id,
        account=account,
    )
    if not state.is_ready:
        return

    path = state_path()
    write_json_atomically(path, asdict(state))


def clear_last_playback() -> None:
    try:
        state_path().unlink(missing_ok=True)
    except OSError:
        pass


@dataclass(frozen=True)
class OutputVolumes:
    speaker: VolumeState
    bluetooth: VolumeState

    def for_output(self, output: str) -> VolumeState:
        return self.bluetooth if output == "bluetooth" else self.speaker

    def with_level(self, output: str, volume: VolumeState) -> OutputVolumes:
        return (
            OutputVolumes(self.speaker, volume)
            if output == "bluetooth"
            else OutputVolumes(volume, self.bluetooth)
        )


def _read_volume_file() -> dict:
    try:
        values = json.loads(state_path().with_name("volume.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return values if isinstance(values, dict) else {}


def _volume_from_data(values: object) -> VolumeState | None:
    if not isinstance(values, dict):
        return None
    percent = values.get("percent")
    muted = values.get("muted")
    if type(percent) is not int or not 0 <= percent <= 100 or not isinstance(muted, bool):
        return None
    return VolumeState(percent, muted)


def load_saved_volume() -> VolumeState | None:
    return _volume_from_data(_read_volume_file())


def load_bluetooth_volume(speaker: VolumeState) -> VolumeState:
    return _volume_from_data(_read_volume_file().get("bluetooth")) or VolumeState(
        min(speaker.percent, 10),
        speaker.muted,
    )


def save_output_volumes(profiles: OutputVolumes) -> None:
    write_json_atomically(
        state_path().with_name("volume.json"),
        {**asdict(profiles.speaker), "bluetooth": asdict(profiles.bluetooth)},
    )


def state_path() -> Path:
    configured_path = os.environ.get("ABS_KIDS_PLAYER_STATE_PATH", "").strip()
    if configured_path:
        return Path(configured_path).expanduser()

    configured_dir = os.environ.get("ABS_KIDS_PLAYER_STATE_DIR", "").strip()
    if configured_dir:
        return Path(configured_dir).expanduser() / "player-state.json"

    return STATE_PATH
