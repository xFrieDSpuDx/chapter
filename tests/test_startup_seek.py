from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from abs_kids_player.startup_seek import PREROLL_POLL_SECONDS, can_seek_to, wait_for_resume_seek


class StartupSeekTest(unittest.TestCase):
    def test_waiting_for_seekability_uses_the_existing_preroll_deadline(self) -> None:
        now = 0.0
        intervals = []

        def wait_for_state(interval: float) -> str:
            nonlocal now
            intervals.append(interval)
            now += interval
            return "timeout"

        try_seek = Mock(return_value=None)
        result = wait_for_resume_seek(
            wait_for_state, try_seek, clock=lambda: now, timeout=5, cancelled=None
        )

        self.assertEqual(result, "timeout")
        self.assertEqual(intervals[0], 0.0)
        self.assertLessEqual(max(intervals), PREROLL_POLL_SECONDS)
        self.assertAlmostEqual(sum(intervals), 5.0)
        self.assertEqual(try_seek.call_count, len(intervals) - 1)

    def test_seekability_query_requires_audio_and_the_requested_time_in_range(self) -> None:
        parse_seeking = Mock()
        query = SimpleNamespace(parse_seeking=parse_seeking)
        gst = SimpleNamespace(
            SECOND=1000,
            Format=SimpleNamespace(TIME="time"),
            Query=SimpleNamespace(new_seeking=Mock(return_value=query)),
        )
        playbin = SimpleNamespace(get_property=Mock(return_value=0), query=Mock(return_value=True))

        self.assertFalse(can_seek_to(gst, playbin, 42))
        gst.Query.new_seeking.assert_not_called()
        playbin.get_property.return_value = 1
        for response, accepted in (
            (("bytes", True, 0, -1), False),
            (("time", False, 0, -1), False),
            (("time", True, 45000, -1), False),
            (("time", True, 0, 40000), False),
            (("time", True, 20000, 50000), True),
            (("time", True, 0, -1), True),
        ):
            with self.subTest(response=response):
                parse_seeking.return_value = response
                self.assertEqual(can_seek_to(gst, playbin, 42), accepted)

        playbin.query.side_effect = RuntimeError("query unavailable")
        self.assertFalse(can_seek_to(gst, playbin, 42))
