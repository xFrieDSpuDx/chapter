from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from abs_kids_player.bedtime import BedtimeTimer, night_hours_active, night_mode_active
from abs_kids_player.models import AudioTrack, PlaybackSession
from abs_kids_player.playback import MAX_EFFECTIVE_VOLUME, GStreamerPlayback
from abs_kids_player.playback_owner import PlaybackOwner


class BedtimeTest(unittest.TestCase):
    def test_one_off_timer_fades_and_optionally_rewinds_to_the_fade_start(self) -> None:
        timer = BedtimeTimer()
        timer.arm("book", 100, 15, 0, rewind=True)
        before = timer.update(session_id="book", now=991, position=20, speed=1, playing=True)
        fading = timer.update(session_id="book", now=996, position=25, speed=1, playing=True)
        done = timer.update(session_id="book", now=1000, position=29, speed=1, playing=True)

        self.assertEqual(before.gain, 1.0)
        self.assertEqual(fading.gain, 0.5)
        self.assertEqual(done.gain, 0.0)
        self.assertTrue(done.pause)
        self.assertEqual(done.rewind_to, 25)
        self.assertTrue(timer.sleeping)

    def test_end_of_chapter_consumes_playback_time_and_resets_on_book_change(self) -> None:
        timer = BedtimeTimer()
        timer.arm("book", 20, -1, 100, rewind=False)
        self.assertEqual(timer.update(session_id="book", now=50, position=92, speed=2, playing=False).gain, 1)
        self.assertEqual(timer.update(session_id="book", now=51, position=92, speed=2, playing=True).gain, 0.5)
        self.assertTrue(timer.update(session_id="book", now=55, position=100, speed=2, playing=True).pause)
        self.assertFalse(timer.update(session_id="next", now=56, position=0, speed=1, playing=True).pause)
        self.assertFalse(timer.sleeping)

    def test_sleep_rewind_uses_a_hard_pause_for_exact_resume(self) -> None:
        playback = GStreamerPlayback()
        playback.pause = Mock()
        playback.seek_global = Mock()
        owner = PlaybackOwner(playback)
        self.addCleanup(owner.close)

        owner.sleep_at_position(75.5)

        playback.pause.assert_called_once_with(sync=False, keep_audio_alive=False)
        playback.seek_global.assert_called_once_with(75.5)

    def test_end_of_chapter_at_file_boundary_does_not_open_the_next_file(self) -> None:
        playback = GStreamerPlayback()
        playback.session = PlaybackSession(
            "session", "book", "Title", "", 200, 99, "",
            [AudioTrack(1, 0, 100, "first", "One"), AudioTrack(2, 100, 100, "second", "Two")], [],
        )
        playback.Gst = SimpleNamespace(State=SimpleNamespace(PAUSED=1))
        playback.playbin = Mock()
        playback.is_playing = True
        playback.sleep_boundary = 100
        self.assertFalse(playback.advance_within_session())
        self.assertEqual(playback.track_index, 0)
        self.assertEqual(playback.current_time, 100)
        self.assertFalse(playback.is_playing)

        timer = BedtimeTimer()
        timer.arm("session", 0, -1, 100, rewind=False)
        self.assertTrue(timer.update(session_id="session", now=1, position=100, speed=1, playing=False).pause)

    def test_manual_night_mode_overrides_and_returns_to_the_schedule(self) -> None:
        day, night = datetime(2026, 11, 24, 13), datetime(2026, 11, 24, 1)
        self.assertTrue(night_mode_active("on", "", "", day))
        self.assertFalse(night_mode_active("off", "22:00", "07:00", night))
        self.assertTrue(night_mode_active("schedule", "22:00", "07:00", night))
        self.assertFalse(night_mode_active("schedule", "22:00", "07:00", day))

    def test_night_hours_cross_midnight_and_preserve_the_chosen_playback_level(self) -> None:
        self.assertTrue(night_hours_active("22:00", "07:00", datetime(2026, 11, 24, 6, 59)))
        self.assertFalse(night_hours_active("22:00", "07:00", datetime(2026, 11, 24, 7)))
        self.assertTrue(night_hours_active("22:00", "07:00", datetime(2026, 11, 24, 22)))
        self.assertFalse(night_hours_active("22:00", "22:00", datetime(2026, 11, 24, 22)))
        playback = GStreamerPlayback()
        playback.set_volume_state(45, False)
        playback.set_night_volume_limit(20)
        playback.set_sleep_gain(0.5)
        self.assertAlmostEqual(playback.target_effective_volume(), 0.10 * MAX_EFFECTIVE_VOLUME)
        playback.set_night_volume_limit(100)
        playback.set_sleep_gain(1)
        self.assertEqual(playback.volume_percent, 45)
        self.assertAlmostEqual(playback.target_effective_volume(), 0.45 * MAX_EFFECTIVE_VOLUME)
