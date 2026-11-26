import unittest
from datetime import datetime

from abs_kids_player.config import AppConfig
from abs_kids_player.screen_power import screen_off_due


class ScreenPowerTest(unittest.TestCase):
    def test_idle_timeout_counts_only_time_since_start_or_last_dial_input(self):
        noon = datetime(2026, 11, 25, 12)
        config = AppConfig(screen_off_minutes=1)
        self.assertFalse(screen_off_due(AppConfig(), 9000, noon, 100, None))
        self.assertFalse(screen_off_due(config, 159, noon, 100, None))
        self.assertTrue(screen_off_due(config, 160, noon, 100, None))
        self.assertFalse(screen_off_due(config, 209, noon, 100, 150))
        self.assertTrue(screen_off_due(config, 210, noon, 100, 150))

    def test_boot_schedule_boundaries_and_wake_grace(self):
        config = AppConfig(screen_off_start="22:00", screen_off_end="06:30")
        for hour, minute, off in [(21, 59, False), (22, 0, True), (0, 0, True), (6, 29, True), (6, 30, False)]:
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(screen_off_due(config, 10, datetime(2026, 11, 25, hour, minute), 10, None), off)
        midnight = datetime(2026, 11, 25)
        self.assertFalse(screen_off_due(config, 69, midnight, 0, 10))
        self.assertTrue(screen_off_due(config, 70, midnight, 0, 10))

    def test_idle_and_same_day_schedule_can_each_turn_the_screen_off(self):
        config = AppConfig(screen_off_minutes=15, screen_off_start="10:00", screen_off_end="12:00")
        self.assertTrue(screen_off_due(config, 70, datetime(2026, 11, 25, 10), 0, 10))
        self.assertFalse(screen_off_due(config, 70, datetime(2026, 11, 25, 12), 0, 10))
        self.assertTrue(screen_off_due(config, 910, datetime(2026, 11, 25, 12), 0, 10))
