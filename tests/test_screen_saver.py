from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from abs_kids_player.config import (
    CLOCK_FORMAT_24_HOUR,
    DEFAULT_SCREEN_SAVER_DIM_PERCENT,
    SCREEN_SAVER_BOOKS,
    SCREEN_SAVER_CLOCK,
)
from abs_kids_player.display_frames import (
    ScreenSaverFrame,
)
from abs_kids_player.models import Book
from abs_kids_player.screen_saver import (
    DEFAULT_SCREEN_SAVER_IDLE_SECONDS,
    clock_screen_saver_text,
    screen_saver_frame,
    screen_saver_is_active,
    screen_saver_words_for_mode,
)


class ScreenSaverTest(unittest.TestCase):
    def test_screen_saver_starts_after_five_idle_minutes(self) -> None:
        last_input_at = 100.0

        self.assertFalse(
            screen_saver_is_active(
                now=last_input_at + DEFAULT_SCREEN_SAVER_IDLE_SECONDS - 0.1,
                last_input_at=last_input_at,
                idle_seconds=DEFAULT_SCREEN_SAVER_IDLE_SECONDS,
            )
        )
        self.assertTrue(
            screen_saver_is_active(
                now=last_input_at + DEFAULT_SCREEN_SAVER_IDLE_SECONDS,
                last_input_at=last_input_at,
                idle_seconds=DEFAULT_SCREEN_SAVER_IDLE_SECONDS,
            )
        )

    def test_screen_saver_frame_advances_with_idle_time(self) -> None:
        frame = screen_saver_frame(now=305.5, last_input_at=5.0, frames_per_second=8.0)

        self.assertIsInstance(frame, ScreenSaverFrame)
        self.assertEqual(frame.frame_index, 2404)
        self.assertEqual(frame.mode, "chapter")
        self.assertEqual(frame.words, ("chapter",))
        self.assertEqual(frame.brightness_percent, DEFAULT_SCREEN_SAVER_DIM_PERCENT)

    def test_screen_saver_frame_uses_configured_dim_level(self) -> None:
        frame = screen_saver_frame(
            now=305.5,
            last_input_at=5.0,
            brightness_percent=10,
        )

        self.assertEqual(frame.brightness_percent, 10)

    def test_screen_saver_frame_can_use_book_titles(self) -> None:
        frame = screen_saver_frame(
            now=305.5,
            last_input_at=5.0,
            frames_per_second=8.0,
            mode=SCREEN_SAVER_BOOKS,
            words=("Matilda", "The Hobbit"),
        )

        self.assertEqual(frame.mode, SCREEN_SAVER_BOOKS)
        self.assertEqual(frame.words, ("Matilda", "The Hobbit"))

    def test_screen_saver_frame_can_use_clock(self) -> None:
        frame = screen_saver_frame(
            now=305.5,
            last_input_at=5.0,
            frames_per_second=8.0,
            mode=SCREEN_SAVER_CLOCK,
        )

        self.assertEqual(frame.mode, SCREEN_SAVER_CLOCK)
        self.assertRegex(frame.clock_text, r"^\d{1,2}:\d{2} [AP]M$")

    def test_screen_saver_frame_uses_configured_clock_format(self) -> None:
        with patch("abs_kids_player.screen_saver.datetime", wraps=datetime) as date:
            date.now.return_value = datetime(2026, 6, 9, 19, 5)
            frame = screen_saver_frame(
                now=305.5,
                last_input_at=5.0,
                mode=SCREEN_SAVER_CLOCK,
                clock_format=CLOCK_FORMAT_24_HOUR,
            )

        self.assertEqual(frame.clock_text, "19:05")

    def test_clock_frame_stays_equal_within_the_same_minute(self) -> None:
        with patch("abs_kids_player.screen_saver.datetime", wraps=datetime) as date:
            date.now.return_value = datetime(2026, 6, 9, 19, 5)
            first = screen_saver_frame(now=305, last_input_at=5, mode=SCREEN_SAVER_CLOCK)
            second = screen_saver_frame(now=330, last_input_at=5, mode=SCREEN_SAVER_CLOCK)

        self.assertEqual(first, second)

    def test_screen_saver_words_for_books_uses_library_titles(self) -> None:
        books = [
            Book(
                id="book-1", title="The Hobbit", author="J.R.R. Tolkien", duration=1, cover_url=""
            ),
            Book(
                id="book-2",
                title="The Wild Robot Escapes",
                author="Peter Brown",
                duration=1,
                cover_url="",
                series_name="The Wild Robot",
            ),
        ]

        self.assertEqual(
            screen_saver_words_for_mode(SCREEN_SAVER_BOOKS, books),
            ("The Hobbit", "The Wild Robot: The Wild Robot Escapes"),
        )
        self.assertEqual(screen_saver_words_for_mode(SCREEN_SAVER_BOOKS, []), ("chapter",))

    def test_clock_screen_saver_text_uses_am_pm(self) -> None:
        self.assertEqual(clock_screen_saver_text(datetime(2026, 6, 9, 7, 5)), "7:05 AM")
        self.assertEqual(clock_screen_saver_text(datetime(2026, 6, 9, 19, 5)), "7:05 PM")

    def test_clock_screen_saver_text_uses_24_hour_time_at_midnight_and_noon(self) -> None:
        for hour, expected in ((0, "00:05"), (7, "07:05"), (12, "12:05"), (19, "19:05")):
            with self.subTest(hour=hour):
                self.assertEqual(
                    clock_screen_saver_text(
                        datetime(2026, 6, 9, hour, 5), clock_format=CLOCK_FORMAT_24_HOUR
                    ),
                    expected,
                )

    def test_screen_saver_is_disabled_with_zero_idle_seconds(self) -> None:
        self.assertFalse(screen_saver_is_active(now=999.0, last_input_at=0.0, idle_seconds=0.0))
