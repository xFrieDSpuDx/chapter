from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable

from .storage import ensure_storage_directory, write_json_atomically


CONFIG_DIR = Path.home() / ".config" / "abs-kids-player"
CONFIG_PATH = CONFIG_DIR / "config.json"
SCREEN_SAVER_CHAPTER = "chapter"
SCREEN_SAVER_BOOKS = "books"
SCREEN_SAVER_CLOCK = "clock"
SCREEN_SAVER_MODES = (SCREEN_SAVER_CHAPTER, SCREEN_SAVER_BOOKS, SCREEN_SAVER_CLOCK)
CLOCK_FORMAT_12_HOUR = "12-hour"
CLOCK_FORMAT_24_HOUR = "24-hour"
LIBRARY_SORT_TITLE = "title"
LIBRARY_SORT_AUTHOR = "author"
LIBRARY_SORT_AUTHOR_FIRST = "author_first"
LIBRARY_SORT_MODES = (LIBRARY_SORT_TITLE, LIBRARY_SORT_AUTHOR, LIBRARY_SORT_AUTHOR_FIRST)
PLAYBACK_SPEEDS = (1.0, 1.25, 1.5, 1.75, 2.0)
DEFAULT_PLAYBACK_SPEED = 1.0
DEFAULT_PREFER_DOWNLOADED = True
SCREEN_SAVER_DIM_LEVELS = (5, 10, 15, 20, 25, 35, 50)
SCREEN_OFF_DELAYS = (0, 1, 5, 15, 30, 60)
DEFAULT_SCREEN_SAVER_DIM_PERCENT = 15
DEFAULT_CONTROL_CLICK_VOLUME_PERCENT = 70
DEFAULT_SPOKEN_NAVIGATION_VOLUME_PERCENT = 35
DEFAULT_SLEEP_TIMER_MINUTES = 30
MIN_SLEEP_TIMER_MINUTES = 1
MAX_SLEEP_TIMER_MINUTES = 240
NIGHT_MODES = ("schedule", "on", "off")


class ConfigReadError(OSError):
    """Existing settings need recovery before they can be changed."""


@dataclass(frozen=True)
class PodcastConfig:
    url: str
    title: str = ""
    author: str = ""
    feed_url: str = ""
    book_id: str = ""


def default_podcasts() -> list[PodcastConfig]:
    return []


@dataclass
class AppConfig:
    server_url: str = ""
    token: str = ""
    refresh_token: str = ""
    library_id: str = ""
    username: str = ""
    control_click_enabled: bool = True
    spoken_navigation_enabled: bool = False
    library_sort_mode: str = LIBRARY_SORT_TITLE
    playback_speed: float = DEFAULT_PLAYBACK_SPEED
    screen_saver_mode: str = SCREEN_SAVER_CHAPTER
    screen_saver_dim_percent: int = DEFAULT_SCREEN_SAVER_DIM_PERCENT
    screen_saver_clock_format: str = CLOCK_FORMAT_12_HOUR
    sleep_timer_enabled: bool = False
    sleep_timer_minutes: int = DEFAULT_SLEEP_TIMER_MINUTES
    podcasts: list[PodcastConfig] = field(default_factory=default_podcasts)
    control_click_volume_percent: int = DEFAULT_CONTROL_CLICK_VOLUME_PERCENT
    spoken_navigation_volume_percent: int = DEFAULT_SPOKEN_NAVIGATION_VOLUME_PERCENT
    pinned_shelf: str = ""
    skip_back_seconds: int = 15
    skip_forward_seconds: int = 30
    sleep_rewind: bool = False
    night_start: str = ""
    night_end: str = ""
    night_dim_percent: int = 25
    night_volume_limit: int = 20
    prefer_downloaded: bool = DEFAULT_PREFER_DOWNLOADED
    download_limit_mib: int = 4096
    library_name: str = ""
    night_mode: str = "schedule"
    screen_off_minutes: int = 0
    screen_off_start: str = ""
    screen_off_end: str = ""

    @property
    def is_ready(self) -> bool:
        return bool(self.server_url.strip() and self.token.strip())


def _read_settings() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected a settings object")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ConfigReadError("Cannot read Chapter settings. Check config.json or restore a known-good copy.") from error

def load_config() -> AppConfig:
    return _config_from_settings(_read_settings())


def _config_from_settings(data: dict) -> AppConfig:
    screen_off_minutes = data.get("screen_off_minutes", 0)
    return AppConfig(
        server_url=str(data.get("server_url", "")),
        token=str(data.get("token", "")),
        refresh_token=str(data.get("refresh_token", "")),
        library_id=str(data.get("library_id", "")),
        library_name=str(data.get("library_name") or ""),
        username=str(data.get("username", "")),
        control_click_enabled=bool(data.get("control_click_enabled", True)),
        spoken_navigation_enabled=bool(data.get("spoken_navigation_enabled", False)),
        control_click_volume_percent=valid_feedback_volume(
            data.get("control_click_volume_percent"), DEFAULT_CONTROL_CLICK_VOLUME_PERCENT
        ),
        spoken_navigation_volume_percent=valid_feedback_volume(
            data.get("spoken_navigation_volume_percent"), DEFAULT_SPOKEN_NAVIGATION_VOLUME_PERCENT
        ),
        library_sort_mode=valid_library_sort_mode(str(data.get("library_sort_mode", ""))),
        pinned_shelf=str(data.get("pinned_shelf") or ""),
        prefer_downloaded=data.get("prefer_downloaded", DEFAULT_PREFER_DOWNLOADED) is True,
        download_limit_mib=valid_download_limit(data.get("download_limit_mib")),
        skip_back_seconds=valid_skip_seconds(data.get("skip_back_seconds"), 15),
        skip_forward_seconds=valid_skip_seconds(data.get("skip_forward_seconds"), 30),
        sleep_rewind=bool(data.get("sleep_rewind", False)),
        night_mode=valid_night_mode(data.get("night_mode")),
        night_start=valid_clock_time(data.get("night_start")),
        night_end=valid_clock_time(data.get("night_end")),
        night_dim_percent=valid_feedback_volume(data.get("night_dim_percent"), 25),
        night_volume_limit=max(1, valid_feedback_volume(data.get("night_volume_limit"), 20)),
        playback_speed=valid_playback_speed(data.get("playback_speed")),
        screen_saver_mode=valid_screen_saver_mode(str(data.get("screen_saver_mode", ""))),
        screen_saver_dim_percent=valid_screen_saver_dim_percent(data.get("screen_saver_dim_percent")),
        screen_saver_clock_format=valid_clock_format(data.get("screen_saver_clock_format")),
        screen_off_minutes=screen_off_minutes if valid_screen_off_delay(screen_off_minutes) else 0,
        screen_off_start=valid_clock_time(data.get("screen_off_start")),
        screen_off_end=valid_clock_time(data.get("screen_off_end")),
        sleep_timer_enabled=bool(data.get("sleep_timer_enabled", False)),
        sleep_timer_minutes=valid_sleep_timer_minutes(data.get("sleep_timer_minutes")),
        podcasts=podcast_configs_from_json(data["podcasts"]) if "podcasts" in data else default_podcasts(),
    )


@contextmanager
def _config_transaction():
    # The containing directory stays in place when an atomic save replaces config.json.
    directory = config_path().parent
    if not directory.exists():
        ensure_storage_directory(directory)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def save_config(config: AppConfig) -> None:
    """Replace the whole configuration for an explicit device reset or initial setup."""
    with _config_transaction():
        load_config()  # Preserve an unreadable existing configuration for recovery.
        write_json_atomically(config_path(), asdict(config), durable=True)


def update_config(change: Callable[[AppConfig], AppConfig]) -> tuple[AppConfig, AppConfig]:
    with _config_transaction():
        data = _read_settings()
        previous = _config_from_settings(data)
        updated = change(previous)
        if updated != previous:
            original = asdict(previous)
            for key, value in asdict(updated).items():
                if value != original[key]:
                    data[key] = value
            write_json_atomically(config_path(), data, durable=True)
        return previous, updated


def saved_auth_tokens(server_url: str, username: str) -> tuple[str, str]:
    config = load_config()
    if not same_login(config, server_url, username):
        return "", ""
    return config.token, config.refresh_token


def update_auth_tokens(server_url: str, username: str, token: str, refresh_token: str) -> bool:
    def rotate(current: AppConfig) -> AppConfig:
        if same_login(current, server_url, username):
            return replace(current, token=token, refresh_token=refresh_token)
        return current

    previous, _ = update_config(rotate)
    return same_login(previous, server_url, username)


def remember_verified_username(server_url: str, token: str, username: str) -> None:
    def remember(current: AppConfig) -> AppConfig:
        if current.server_url.rstrip("/") == server_url.rstrip("/") and current.token == token:
            if current.username.strip().casefold() in ("", "unknown user"):
                return replace(current, username=username)
        return current

    update_config(remember)


def same_login(config: AppConfig, server_url: str, username: str) -> bool:
    configured_server = config.server_url.strip().rstrip("/").casefold()
    expected_server = server_url.strip().rstrip("/").casefold()
    configured_username = config.username.strip().casefold()
    expected_username = username.strip().casefold()
    return configured_server == expected_server and configured_username == expected_username


def config_path() -> Path:
    configured_path = os.environ.get("ABS_KIDS_PLAYER_CONFIG_PATH", "").strip()
    if configured_path:
        return Path(configured_path).expanduser()

    configured_dir = os.environ.get("ABS_KIDS_PLAYER_CONFIG_DIR", "").strip()
    if configured_dir:
        return Path(configured_dir).expanduser() / "config.json"

    return CONFIG_PATH


def valid_screen_saver_mode(value: str) -> str:
    return value if value in SCREEN_SAVER_MODES else SCREEN_SAVER_CHAPTER


def valid_clock_format(value: object) -> str:
    return CLOCK_FORMAT_24_HOUR if value == CLOCK_FORMAT_24_HOUR else CLOCK_FORMAT_12_HOUR


def valid_library_sort_mode(value: str) -> str:
    return value if value in LIBRARY_SORT_MODES else LIBRARY_SORT_TITLE


def valid_playback_speed(value: object) -> float:
    try:
        speed = float(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_PLAYBACK_SPEED
    return speed if speed in PLAYBACK_SPEEDS else DEFAULT_PLAYBACK_SPEED


def valid_screen_saver_dim_percent(value: object) -> int:
    try:
        percent = int(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_SCREEN_SAVER_DIM_PERCENT
    return percent if percent in SCREEN_SAVER_DIM_LEVELS else DEFAULT_SCREEN_SAVER_DIM_PERCENT


def valid_feedback_volume(value: object, fallback: int) -> int:
    try:
        percent = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return min(max(0, percent), 100)


def valid_sleep_timer_minutes(value: object) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_SLEEP_TIMER_MINUTES
    return min(max(minutes, MIN_SLEEP_TIMER_MINUTES), MAX_SLEEP_TIMER_MINUTES)


def valid_skip_seconds(value: object, default: int) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(120, max(5, seconds))


def valid_download_limit(value: object) -> int:
    try:
        return min(max(1, int(str(value))), 1_048_576)
    except ValueError:
        return 4096


def valid_night_mode(value: object) -> str:
    return value if isinstance(value, str) and value in NIGHT_MODES else "schedule"


def valid_clock_time(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) else ""


def valid_screen_off_delay(value: object) -> bool:
    return type(value) is int and value in SCREEN_OFF_DELAYS


def valid_screen_off_hours(start: object, end: object) -> bool:
    return (start == end == "") or bool(valid_clock_time(start) and valid_clock_time(end) and start != end)


def podcast_configs_from_json(value: object) -> list[PodcastConfig]:
    if not isinstance(value, list):
        return default_podcasts()

    podcasts: list[PodcastConfig] = []
    for item in value:
        if isinstance(item, str):
            url = item.strip()
            if url:
                podcasts.append(PodcastConfig(url=url))
            continue
        if not isinstance(item, dict):
            continue
        podcast = PodcastConfig(
            url=str(item.get("url", "")).strip(),
            title=str(item.get("title", "")).strip(),
            author=str(item.get("author", "")).strip(),
            feed_url=str(item.get("feed_url", "")).strip(),
            book_id=str(item.get("book_id", "")).strip(),
        )
        if podcast.url or podcast.feed_url:
            podcasts.append(podcast)
    return podcasts
