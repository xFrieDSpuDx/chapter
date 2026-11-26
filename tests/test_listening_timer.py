from __future__ import annotations

import unittest

from abs_kids_player.listening_timer import ListeningSleepTimer


class ListeningTimerTest(unittest.TestCase):
    def test_listening_sleep_timer_counts_only_active_playback(self) -> None:
        timer = ListeningSleepTimer()

        self.assertEqual(timer.update(30, 30, True, 1, "session-1", True), "")
        self.assertEqual(timer.update(50, 20, True, 1, "session-1", False), "")
        self.assertEqual(timer.listened_seconds, 30)
        self.assertEqual(timer.update(80, 30, True, 1, "session-1", True), "prompt")
        self.assertEqual(timer.countdown_seconds(80), 30)

    def test_listening_sleep_timer_counts_down_then_sleeps(self) -> None:
        timer = ListeningSleepTimer()
        timer.update(60, 60, True, 1, "session-1", True)

        self.assertEqual(timer.update(89.1, 0, True, 1, "session-1", False), "")
        self.assertEqual(timer.countdown_seconds(89.1), 1)
        self.assertEqual(timer.update(90, 0, True, 1, "session-1", False), "sleep")
        self.assertTrue(timer.sleeping)
        self.assertEqual(timer.sleep_started_at, 90)

    def test_listening_sleep_timer_confirm_and_wake_reset_interval(self) -> None:
        timer = ListeningSleepTimer()
        timer.update(60, 60, True, 1, "session-1", True)
        timer.confirm()

        self.assertFalse(timer.prompt_active)
        self.assertEqual(timer.listened_seconds, 0)

        timer.update(120, 60, True, 1, "session-1", True)
        timer.update(150, 0, True, 1, "session-1", False)
        self.assertTrue(timer.wake())
        self.assertFalse(timer.sleeping)
        self.assertEqual(timer.listened_seconds, 0)

    def test_disabling_active_sleep_timer_cancels_prompt(self) -> None:
        timer = ListeningSleepTimer()
        timer.update(60, 60, True, 1, "session-1", True)

        event = timer.update(61, 1, False, 1, "session-1", False)

        self.assertEqual(event, "cancel")
        self.assertFalse(timer.prompt_active)
        self.assertFalse(timer.sleeping)
