"""Exercise the writers used by the separate OLED and setup processes."""

import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abs_kids_player import config
from abs_kids_player.setup_server import save_playback_speed_setting, save_screen_saver_setting


def save_speed(holding, release):
    write = config.write_json_atomically

    def pause_write(*args, **kwargs):
        holding.set()
        if not release.wait(10):
            raise TimeoutError("config writer did not resume")
        return write(*args, **kwargs)

    with patch.object(config, "write_json_atomically", side_effect=pause_write):
        save_playback_speed_setting(1.5)


def save_other_settings(starting, finished):
    starting.set()
    save_screen_saver_setting("clock", clock_format="24-hour")
    config.update_auth_tokens("server", "listener", "rotated", "new-refresh")
    finished.set()


class ConfigTransactionTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "config.json"
        self.enterContext(patch.dict(os.environ, {"ABS_KIDS_PLAYER_CONFIG_PATH": str(self.path)}))

    def test_two_processes_save_independent_fields_and_rotated_tokens(self):
        config.save_config(config.AppConfig(server_url="server", username="listener", token="old"))
        context = multiprocessing.get_context("spawn")
        holding, release, starting, finished = [context.Event() for _ in range(4)]
        speed = context.Process(target=save_speed, args=(holding, release))
        other = context.Process(target=save_other_settings, args=(starting, finished))
        speed.start()
        try:
            self.assertTrue(holding.wait(10))
            other.start()
            self.assertTrue(starting.wait(10))
            self.assertFalse(finished.wait(0.25))
        finally:
            release.set()
            for writer in (speed, other):
                if writer.pid:
                    writer.join(10)
                    if writer.is_alive():
                        writer.terminate()
                        writer.join()
        self.assertEqual((speed.exitcode, other.exitcode), (0, 0))
        saved = config.load_config()
        self.assertEqual((saved.playback_speed, saved.screen_saver_clock_format), (1.5, "24-hour"))
        self.assertEqual((saved.token, saved.refresh_token), ("rotated", "new-refresh"))

    def test_scoped_save_keeps_unrelated_legacy_or_unknown_fields(self):
        import json

        self.path.write_text('{"future_preference":"keep","podcasts":["podcast:legacy"]}')
        save_playback_speed_setting(1.5)
        self.assertEqual(
            json.loads(self.path.read_bytes()),
            {
                "playback_speed": 1.5,
                "future_preference": "keep",
                "podcasts": ["podcast:legacy"],
            },
        )

    def test_invalid_existing_file_is_preserved_even_on_full_save(self):
        for data in (b"[]", b"null", b"{", b"\xff"):
            with self.subTest(data=data):
                self.path.write_bytes(data)
                with self.assertRaises(config.ConfigReadError):
                    save_playback_speed_setting(2)
                with self.assertRaises(config.ConfigReadError):
                    config.save_config(config.AppConfig())
                self.assertEqual(self.path.read_bytes(), data)

    def test_io_error_leaves_saved_settings_available_on_retry(self):
        config.save_config(config.AppConfig(playback_speed=1.5))
        with patch.object(Path, "read_text", side_effect=PermissionError):
            with self.assertRaises(config.ConfigReadError):
                save_screen_saver_setting("clock")
        self.assertEqual(config.load_config().playback_speed, 1.5)
