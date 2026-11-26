from __future__ import annotations

import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from abs_kids_player.audio_output_test import AUDIO_TEST_COMMAND, AudioOutputTest, play_output_test
from abs_kids_player.audio_output_views import render_audio_output
from abs_kids_player.models import PlaybackSession
from abs_kids_player.oled_service import handle_web_player_commands, web_status_for_playback_state
from abs_kids_player.playback_owner import PlaybackSnapshot
from abs_kids_player.player_control import WebPlayerCommand, WebPlayerStatus
from abs_kids_player.rotary_ui import ApplianceMenu
from abs_kids_player.setup_server import SetupHandler


class AudioOutputTestCase(unittest.TestCase):
    def test_output_test_uses_the_chosen_audio_route(self):
        with (
            patch("abs_kids_player.audio_output_test.preferred_bluetooth_alsa_device", return_value="bluealsa:DEV=AA"),
            patch("abs_kids_player.audio_output_test.subprocess.run") as run,
        ):
            self.assertEqual(play_output_test(1, 4), "applied")
            self.assertEqual(play_output_test(2, 4), "applied")
            self.assertEqual([entry.args[0][3] for entry in run.call_args_list],
                             ["default", "plug:bluealsa:DEV=AA"])
            run.side_effect = subprocess.TimeoutExpired("aplay", 5)
            self.assertEqual(play_output_test(1, 4), "audio_error")

    def test_missing_bluetooth_does_not_fall_back_to_the_speaker(self):
        with (
            patch("abs_kids_player.audio_output_test.preferred_bluetooth_alsa_device", return_value=""),
            patch("abs_kids_player.audio_output_test.subprocess.run") as run,
        ):
            self.assertEqual(play_output_test(2, 4), "disconnected")
            run.assert_not_called()

    def test_oled_audio_command_applies_mute_night_cap_and_active_playback_gate(self):
        command = WebPlayerCommand(AUDIO_TEST_COMMAND, 1, id="audio")
        playback = SimpleNamespace(is_playing=False, loading_book=False)
        test = AudioOutputTest()

        def send(*, muted=False, night_volume_limit=100):
            with patch("abs_kids_player.oled_service.consume_web_player_commands", return_value=[command]):
                handle_web_player_commands(playback, ApplianceMenu([]), 95, muted,
                                           audio_test=test, night_volume_limit=night_volume_limit)

        with (
            patch("abs_kids_player.audio_output_test.play_output_test", return_value="applied") as play,
            patch("abs_kids_player.audio_output_test.save_command_result") as report,
        ):
            send(night_volume_limit=3)
            test.worker.join(timeout=2)
            play.assert_called_once_with(1, 3, "")
            report.assert_called_with(command, "applied")
            send(muted=True)
            report.assert_called_with(command, "muted")
            playback.is_playing = True
            send()
            report.assert_called_with(command, "busy")
            play.assert_called_once()

    def test_http_queues_audio_test_and_renders_confirmed_route(self):
        handler = object.__new__(SetupHandler)
        handler.post_fields = Mock(return_value={"output": ["bluetooth"]})
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        with patch("abs_kids_player.setup_server.queue_web_player_command",
                   return_value=SimpleNamespace(id="a" * 32)) as queue:
            handler.handle_audio_test_post()
        handler.send_response.assert_called_once_with(303)
        queue.assert_called_once_with(AUDIO_TEST_COMMAND, 2)

        session = PlaybackSession("session", "book-1", "Book", "Author", 60, 0, "", [], [])
        for device, route in (("", "speaker"), ("bluealsa:DEV=AA", "bluetooth")):
            state = PlaybackSnapshot(session=session, audio_output_device=device)
            status = web_status_for_playback_state(state, 30, False)
            self.assertEqual(status.audio_output, route)
        self.assertEqual(web_status_for_playback_state(PlaybackSnapshot(), 30, False).audio_output, "")
        page = render_audio_output(WebPlayerStatus(audio_output="bluetooth", updated_at=time.time()), "Connected to <speaker>",
                                   "csrf", "request", "muted")
        self.assertIn("<dt>Playback output</dt><dd>Bluetooth</dd>", page)
        self.assertIn("Connected to &lt;speaker&gt;", page)
        self.assertIn("Unmute the player", page)
