"""Track active listening time and decide when to prompt or sleep."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


DEFAULT_SLEEP_CONFIRM_SECONDS = 30
SleepTimerEvent = Literal["", "prompt", "sleep", "cancel"]


@dataclass
class ListeningSleepTimer:
    listened_seconds: float = 0.0
    session_id: str = ""
    prompt_deadline: float = 0.0
    sleeping: bool = False
    sleep_started_at: float = 0.0

    @property
    def prompt_active(self) -> bool:
        return self.prompt_deadline > 0

    def update(
        self,
        now: float,
        elapsed_seconds: float,
        enabled: bool,
        minutes: int,
        session_id: str,
        is_playing: bool,
    ) -> SleepTimerEvent:
        if not enabled or not session_id:
            was_active = self.prompt_active or self.sleeping
            self.reset(session_id)
            return "cancel" if was_active else ""

        if session_id != self.session_id:
            self.reset(session_id)

        if self.sleeping:
            return ""

        if self.prompt_active:
            if now >= self.prompt_deadline:
                self.prompt_deadline = 0.0
                self.sleeping = True
                self.sleep_started_at = now
                return "sleep"
            return ""

        if is_playing:
            self.listened_seconds += max(0.0, elapsed_seconds)
        if self.listened_seconds < max(1, minutes) * 60:
            return ""

        self.prompt_deadline = now + DEFAULT_SLEEP_CONFIRM_SECONDS
        return "prompt"

    def countdown_seconds(self, now: float) -> int:
        if not self.prompt_active:
            return 0
        return max(0, math.ceil(self.prompt_deadline - now))

    def confirm(self) -> None:
        self.listened_seconds = 0.0
        self.prompt_deadline = 0.0
        self.sleeping = False
        self.sleep_started_at = 0.0

    def wake(self) -> bool:
        if not self.sleeping:
            return False
        self.confirm()
        return True

    def reset(self, session_id: str = "") -> None:
        self.session_id = session_id
        self.confirm()
