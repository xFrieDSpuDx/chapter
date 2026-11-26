"""Decide when physical dial activity and local quiet hours allow the panel to sleep."""

from datetime import datetime

from .bedtime import night_hours_active
from .config import AppConfig


SCHEDULE_WAKE_SECONDS = 60


def screen_off_due(
    config: AppConfig, now: float, local_time: datetime, started_at: float, last_dial_at: float | None,
) -> bool:
    idle = now - (started_at if last_dial_at is None else last_dial_at)
    if config.screen_off_minutes and idle >= config.screen_off_minutes * 60:
        return True
    return (last_dial_at is None or idle >= SCHEDULE_WAKE_SECONDS) and night_hours_active(
        config.screen_off_start, config.screen_off_end, local_time,
    )
