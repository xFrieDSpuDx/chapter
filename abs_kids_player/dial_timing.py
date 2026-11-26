"""Measure a physical turn through the OLED write during a requested Pi check."""

import time
from collections.abc import Callable
from pathlib import Path

from .appliance_io import InputEvent
from .display_frames import SCREEN_OFF, DisplayFrame


CAPTURE_MARKER = Path("/run/chapter-dial-timing")
CAPTURE_SECONDS = 90


class DialTiming:
    def __init__(self) -> None:
        self.first: InputEvent | None = None
        self.turns = 0

    def observe(self, event: InputEvent) -> None:
        if self.first is None:
            self.first = event
        self.turns += 1

    def show(self, frame: DisplayFrame, draw: Callable[[DisplayFrame], bool]) -> None:
        event, turns = self.first, self.turns
        self.first, self.turns = None, 0
        if event is None or frame == SCREEN_OFF:
            draw(frame)
            return
        start = time.monotonic()
        written = draw(frame)
        end = time.monotonic()
        if not written:
            return
        # Check the temporary marker after the transfer so it cannot delay this frame.
        try:
            age = time.time() - CAPTURE_MARKER.stat().st_mtime
        except OSError:
            return
        if 0 <= age <= CAPTURE_SECONDS:
            print(f"Dial timing dial={event.name} turns={turns} frame={type(frame).__name__} "
                  f"to_draw_ms={(start - event.received_at) * 1000:.1f} "
                  f"draw_ms={(end - start) * 1000:.1f} to_frame_ms={(end - event.received_at) * 1000:.1f}")
