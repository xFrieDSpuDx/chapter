from __future__ import annotations

import unittest

from abs_kids_player.audio import VolumeState
from abs_kids_player.volume_controls import apply_volume_input, stepped_volume_percent


class VolumeControlsTest(unittest.TestCase):
    def test_steps_are_fine_between_zero_and_ten_and_coarse_above_it(self) -> None:
        for percent, steps, expected in (
            (0, 1, 1),
            (8, 1, 9),
            (9, 1, 10),
            (10, 1, 15),
            (12, -1, 10),
            (10, -1, 9),
            (1, -1, 0),
            (0, -1, 0),
            (99, 1, 100),
        ):
            with self.subTest(percent=percent, steps=steps):
                self.assertEqual(stepped_volume_percent(percent, steps), expected)

    def test_batched_turns_use_the_same_steps_as_individual_detents(self) -> None:
        self.assertEqual(stepped_volume_percent(50, -9), 9)
        self.assertEqual(stepped_volume_percent(9, 3), 20)
        self.assertEqual(stepped_volume_percent(100, -1000), 0)

    def test_software_mute_keeps_the_intentionally_quiet_volume(self) -> None:
        quiet = VolumeState(3)
        muted = apply_volume_input("volume_click", 0, quiet, software_volume_only=True)
        unmuted = apply_volume_input("volume_click", 0, muted, software_volume_only=True)

        self.assertEqual(muted, VolumeState(3, muted=True))
        self.assertEqual(unmuted, quiet)
