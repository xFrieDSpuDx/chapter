import io
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from abs_kids_player.player_control import WebPlayerStatus
from abs_kids_player.web_player_events import stream_player_events


class PlayerEventsTest(unittest.TestCase):
    def test_position_updates_are_throttled_and_pause_is_sent_immediately(self):
        original = WebPlayerStatus(title="Matilda", duration=60, is_playing=True)
        later = replace(original, current_time=1.1)
        paused = replace(later, is_playing=False)
        statuses = [original, replace(original, current_time=0.3), later, paused, paused, paused]
        output = io.BytesIO()
        authorized = Mock(side_effect=[True, True, True, True, False])
        with (patch("abs_kids_player.web_player_events.time.monotonic", side_effect=[0, 0, .3, 1.1, 1.2, 6.2, 12]),
              patch("abs_kids_player.web_player_events.time.sleep")):
            stream_player_events(output, Mock(side_effect=statuses), authorized)
        events = output.getvalue().decode().splitlines()
        updates = [json.loads(event.removeprefix("data: ")) for event in events if event.startswith("data: ")]
        self.assertEqual([(row["currentTime"], row["isPlaying"]) for row in updates],
                         [(0, True), (1.1, True), (1.1, False)])
        self.assertEqual(events.count(": heartbeat"), 1)
        self.assertEqual(authorized.call_count, 5)
