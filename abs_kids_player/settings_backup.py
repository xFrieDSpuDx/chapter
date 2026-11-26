"""Read and write the portable part of a Chapter configuration."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import replace

from . import config
from .catalogue_settings import catalogue_request_key


BACKUP_VERSION = 1
PREVIEW_KEY = secrets.token_bytes(32)
LABELS = {
    "control_click_enabled": "Control knob click",
    "control_click_volume_percent": "Click volume (%)",
    "spoken_navigation_enabled": "Spoken navigation",
    "spoken_navigation_volume_percent": "Speech volume (%)",
    "library_sort_mode": "Library order",
    "pinned_shelf": "Pinned shelf",
    "playback_speed": "Playback speed",
    "screen_saver_mode": "Screen saver",
    "screen_saver_dim_percent": "Screen saver dim (%)",
    "screen_saver_clock_format": "Clock format",
    "screen_off_minutes": "Screen off after (minutes)",
    "screen_off_start": "Screen off from",
    "screen_off_end": "Screen off until",
    "sleep_timer_enabled": "Sleep timer",
    "sleep_timer_minutes": "Sleep timer (minutes)",
    "sleep_rewind": "Rewind after sleep fade",
    "skip_back_seconds": "Skip back (seconds)",
    "skip_forward_seconds": "Skip forward (seconds)",
    "night_mode": "Night mode",
    "night_start": "Night starts",
    "night_end": "Night ends",
    "night_dim_percent": "Night OLED (%)",
    "night_volume_limit": "Night volume limit (%)",
    "prefer_downloaded": "Prefer downloaded books",
    "download_limit_mib": "Download limit (MiB)",
}
BOOLEAN_FIELDS = {
    "control_click_enabled", "spoken_navigation_enabled", "sleep_timer_enabled", "sleep_rewind",
    "prefer_downloaded",
}
NUMBER_RANGES = {
    "control_click_volume_percent": (0, 100),
    "spoken_navigation_volume_percent": (0, 100),
    "sleep_timer_minutes": (config.MIN_SLEEP_TIMER_MINUTES, config.MAX_SLEEP_TIMER_MINUTES),
    "skip_back_seconds": (5, 120),
    "skip_forward_seconds": (5, 120),
    "night_dim_percent": (0, 100),
    "night_volume_limit": (1, 100),
    "download_limit_mib": (1, 1_048_576),
}
CHOICES = {
    "library_sort_mode": config.LIBRARY_SORT_MODES,
    "playback_speed": config.PLAYBACK_SPEEDS,
    "screen_saver_mode": config.SCREEN_SAVER_MODES,
    "screen_saver_dim_percent": config.SCREEN_SAVER_DIM_LEVELS,
    "screen_saver_clock_format": (config.CLOCK_FORMAT_12_HOUR, config.CLOCK_FORMAT_24_HOUR),
    "night_mode": config.NIGHT_MODES,
}


class BackupError(ValueError):
    pass


def backup_payload(settings: config.AppConfig) -> dict:
    return {"version": BACKUP_VERSION, "settings": {name: getattr(settings, name) for name in LABELS}}


def read_backup(raw: str) -> dict:
    if len(raw) > 8192:
        raise BackupError("The settings backup is too large.")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise BackupError("Choose a valid Chapter settings backup (JSON).") from error
    if (
        not isinstance(data, dict) or set(data) != {"version", "settings"}
        or type(data["version"]) is not int or data["version"] != BACKUP_VERSION
    ):
        raise BackupError("This settings backup format is unsupported.")
    values = data["settings"]
    if not isinstance(values, dict) or not values or values.keys() - LABELS.keys():
        raise BackupError("The backup contains unsupported settings.")
    for name, value in values.items():
        if not valid_setting(name, value):
            raise BackupError(f"Invalid value for {LABELS[name]}.")
    if "screen_off_start" in values or "screen_off_end" in values:
        if not config.valid_screen_off_hours(values.get("screen_off_start"), values.get("screen_off_end")):
            raise BackupError("The screen-off schedule needs two different times or two empty times.")
    return values


def valid_setting(name: str, value: object) -> bool:
    if name in BOOLEAN_FIELDS:
        return type(value) is bool
    if name in NUMBER_RANGES:
        minimum, maximum = NUMBER_RANGES[name]
        return type(value) is int and minimum <= value <= maximum
    if name in CHOICES:
        return type(value) in (str, int, float) and value in CHOICES[name]
    if name == "screen_off_minutes":
        return config.valid_screen_off_delay(value)
    if name in ("night_start", "night_end", "screen_off_start", "screen_off_end"):
        return isinstance(value, str) and value == config.valid_clock_time(value)
    return isinstance(value, str) and (
        value == "" or bool(re.fullmatch(r"(?:collections|playlists)/[A-Za-z0-9_-]{1,180}", value))
    )


def preview_ticket(current: config.AppConfig, raw: str) -> str:
    data = json.dumps([catalogue_request_key(current), backup_payload(current), raw], sort_keys=True)
    return hmac.new(PREVIEW_KEY, data.encode(), hashlib.sha256).hexdigest()


def apply_preview(raw: str, ticket: str) -> tuple[config.AppConfig, config.AppConfig]:
    fields = read_backup(raw)

    def apply(current: config.AppConfig) -> config.AppConfig:
        if not re.fullmatch(r"[0-9a-f]{64}", ticket) or not hmac.compare_digest(ticket, preview_ticket(current, raw)):
            raise BackupError("The preview has expired or the settings changed. Preview the backup again.")
        return replace(current, **fields)

    return config.update_config(apply)
