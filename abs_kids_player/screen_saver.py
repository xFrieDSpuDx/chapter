"""Choose the idle-screen content and format its clock."""

from __future__ import annotations

from datetime import datetime

from .config import (
    CLOCK_FORMAT_12_HOUR,
    CLOCK_FORMAT_24_HOUR,
    DEFAULT_SCREEN_SAVER_DIM_PERCENT,
    SCREEN_SAVER_BOOKS,
    SCREEN_SAVER_CHAPTER,
    SCREEN_SAVER_CLOCK,
)
from .display_frames import ScreenSaverFrame
from .models import Book


DEFAULT_SCREEN_SAVER_IDLE_SECONDS = 300.0
DEFAULT_SCREEN_SAVER_FRAMES_PER_SECOND = 8.0


def screen_saver_is_active(now: float, last_input_at: float, idle_seconds: float) -> bool:
    return idle_seconds > 0 and now - last_input_at >= idle_seconds


def screen_saver_frame(
    now: float,
    last_input_at: float,
    frames_per_second: float = DEFAULT_SCREEN_SAVER_FRAMES_PER_SECOND,
    mode: str = SCREEN_SAVER_CHAPTER,
    words: tuple[str, ...] = ("chapter",),
    brightness_percent: int = DEFAULT_SCREEN_SAVER_DIM_PERCENT,
    *,
    clock_format: str = CLOCK_FORMAT_12_HOUR,
) -> ScreenSaverFrame:
    clock_text = ""
    if mode == SCREEN_SAVER_CLOCK:
        clock_text = clock_screen_saver_text(clock_format=clock_format)
        words = ()
    elif mode == SCREEN_SAVER_BOOKS:
        words = words or ("chapter",)
    else:
        mode = SCREEN_SAVER_CHAPTER
        words = ("chapter",)

    elapsed = max(0.0, now - last_input_at)
    return ScreenSaverFrame(
        frame_index=0 if mode == SCREEN_SAVER_CLOCK else int(elapsed * frames_per_second),
        mode=mode,
        words=words,
        clock_text=clock_text,
        brightness_percent=brightness_percent,
    )


def clock_screen_saver_text(
    now: datetime | None = None,
    *,
    clock_format: str = CLOCK_FORMAT_12_HOUR,
) -> str:
    current = now or datetime.now()
    if clock_format == CLOCK_FORMAT_24_HOUR:
        return current.strftime("%H:%M")
    return current.strftime("%I:%M %p").lstrip("0")


def screen_saver_words_for_mode(mode: str, books: list[Book]) -> tuple[str, ...]:
    if mode == SCREEN_SAVER_BOOKS:
        titles = tuple(book.display_title for book in books if book.display_title)
        return titles or ("chapter",)
    return ("chapter",)
