"""One-off sleep plans and a repeating local-time night window."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


FADE_SECONDS = 8.0


def night_hours_active(start: str, end: str, now: datetime) -> bool:
    if not start or not end or start == end:
        return False
    clock = now.strftime("%H:%M")
    if start < end:
        return start <= clock < end
    return clock >= start or clock < end


def night_mode_active(mode: str, start: str, end: str, now: datetime) -> bool:
    if mode == "schedule":
        return night_hours_active(start, end, now)
    return mode == "on"


@dataclass(frozen=True)
class BedtimeUpdate:
    gain: float = 1.0
    pause: bool = False
    rewind_to: float | None = None


class BedtimeTimer:
    def __init__(self) -> None:
        self.cancel()

    def arm(self, session_id: str, now: float, minutes: int, chapter_end: float, rewind: bool) -> None:
        self.cancel()
        self.session_id = session_id
        self.preset_minutes = minutes
        self.deadline = now + minutes * 60 if minutes > 0 else None
        self.chapter_end = chapter_end if minutes < 0 else None
        self.rewind = rewind

    def cancel(self) -> None:
        self.session_id = ""
        self.preset_minutes = 0
        self.deadline: float | None = None
        self.chapter_end: float | None = None
        self.fade_start: float | None = None
        self.rewind = False
        self.sleeping = False
        self.sleep_started_at = 0.0

    def update(self, *, session_id: str, now: float, position: float, speed: float, playing: bool) -> BedtimeUpdate:
        if session_id != self.session_id:
            self.cancel()
            return BedtimeUpdate()
        if not session_id or self.sleeping or (self.deadline is None and self.chapter_end is None):
            return BedtimeUpdate()
        if self.deadline is not None:
            remaining = self.deadline - now
        else:
            if not playing and position < self.chapter_end:
                return BedtimeUpdate()
            remaining = (self.chapter_end - position) / max(0.1, speed)
        if remaining <= 0:
            self.sleeping = True
            self.sleep_started_at = now
            return BedtimeUpdate(0.0, pause=True, rewind_to=self.fade_start if self.rewind else None)
        if not playing or remaining > FADE_SECONDS:
            return BedtimeUpdate()
        if self.fade_start is None:
            self.fade_start = position
        return BedtimeUpdate(remaining / FADE_SECONDS)
