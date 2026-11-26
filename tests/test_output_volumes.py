import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from abs_kids_player.audio import VolumeState
from abs_kids_player.playback import GStreamerPlayback
from abs_kids_player.player_state import (
    OutputVolumes,
    load_bluetooth_volume,
    load_saved_volume,
    save_output_volumes,
)


class OutputVolumesTest(unittest.TestCase):
    def test_legacy_volume_migrates_to_speaker_and_both_levels_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {
            "ABS_KIDS_PLAYER_STATE_PATH": f"{folder}/player-state.json",
        }):
            (Path(folder) / "volume.json").write_text('{"percent": 3, "muted": false}')
            speaker = load_saved_volume()
            self.assertEqual(speaker, VolumeState(3))
            self.assertEqual(load_bluetooth_volume(speaker), VolumeState(3))
            bluetooth = VolumeState(26, True)
            save_output_volumes(OutputVolumes(speaker, bluetooth))
            self.assertEqual(load_saved_volume(), speaker)
            self.assertEqual(load_bluetooth_volume(speaker), bluetooth)

    @patch("abs_kids_player.playback.preferred_bluetooth_alsa_device", return_value="bluealsa:DEV=headphones")
    def test_routing_restores_destination_level_and_keeps_the_night_limit(self, find_output):
        player = GStreamerPlayback()
        player.configure_output_volumes(OutputVolumes(VolumeState(3), VolumeState(35)))
        player.Gst = Mock()
        player.playbin = Mock()
        player.is_playing = True
        player.night_volume_limit = 8
        self.assertEqual(player.volume_percent, 3)

        player.set_bluetooth_output_enabled(True)
        player.configure_audio_sink()
        self.assertEqual((player.volume_output, player.volume_percent, player.target_effective_volume()),
                         ("bluetooth", 35, .08))
        player.set_volume_state(22, True)
        player.set_bluetooth_output_enabled(False)
        player.configure_audio_sink()
        self.assertEqual((player.volume_output, player.volume_percent, player.muted), ("speaker", 3, False))

        player.set_bluetooth_output_enabled(True)
        player.configure_audio_sink()
        self.assertEqual((player.volume_percent, player.muted), (22, True))
        find_output.return_value = ""
        player.configure_audio_sink()
        self.assertEqual((player.volume_output, player.volume_percent, player.muted), ("speaker", 3, False))

    def test_late_speaker_volume_command_does_not_overwrite_headphones(self):
        player = GStreamerPlayback()
        player.configure_output_volumes(OutputVolumes(VolumeState(3), VolumeState(12)))
        player.set_bluetooth_output_enabled(True)
        player.set_volume_for_output("speaker", 6, False)
        self.assertEqual((player.volume_output, player.volume_percent), ("bluetooth", 12))
        player.set_bluetooth_output_enabled(False)
        self.assertEqual((player.volume_output, player.volume_percent), ("speaker", 6))
