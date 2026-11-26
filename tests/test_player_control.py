from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from abs_kids_player import player_control
from abs_kids_player.oled_service import handle_web_player_commands
from abs_kids_player.playback_owner import PlaybackOwner
from abs_kids_player.player_control import (
    WebCommandInbox,
    WebPlayerStatus,
    consume_web_player_commands,
    load_web_player_status,
    queue_web_player_command,
    save_web_player_status,
)
from abs_kids_player.rotary_ui import ApplianceMenu


class PlayerControlTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.commands_path = Path(directory) / "commands.json"
        self.status_path = Path(directory) / "status.json"
        self.enterContext(patch.object(player_control, "commands_path", return_value=self.commands_path))
        self.enterContext(patch.object(player_control, "status_path", return_value=self.status_path))

    def test_enqueue_during_dequeue_does_not_lose_the_new_command(self) -> None:
        original = queue_web_player_command("pause")
        consumer_read = threading.Event()
        producer_waiting_for_lock = threading.Event()
        release_consumer = threading.Event()
        original_read = player_control._read_command_data
        original_flock = player_control.fcntl.flock

        def read(path):
            entries = original_read(path)
            if not consumer_read.is_set():
                consumer_read.set()
                if not release_consumer.wait(2):
                    raise TimeoutError("Consumer was not released")
            return entries

        def flock(descriptor, operation):
            if consumer_read.is_set():
                producer_waiting_for_lock.set()
            return original_flock(descriptor, operation)

        with (
            ThreadPoolExecutor(max_workers=2) as pool,
            patch.object(player_control, "_read_command_data", side_effect=read),
            patch.object(player_control.fcntl, "flock", side_effect=flock),
        ):
            consumed = pool.submit(consume_web_player_commands)
            try:
                self.assertTrue(consumer_read.wait(1))
                enqueued = pool.submit(queue_web_player_command, "play")
                self.assertTrue(producer_waiting_for_lock.wait(1))
            finally:
                release_consumer.set()
            self.assertEqual(consumed.result(timeout=2), [original])
            new_command = enqueued.result(timeout=2)

        self.assertEqual(consume_web_player_commands(), [new_command])
        self.assertEqual(consume_web_player_commands(), [])

    def test_results_distinguish_rejected_superseded_and_applied(self) -> None:
        rejected = queue_web_player_command("play")
        superseded = queue_web_player_command("volume", 5)
        applied = queue_web_player_command("volume", 7)
        playback = Mock(spec=PlaybackOwner)
        playback.resume.return_value = False
        playback.is_playing = False
        result: Future[None] = Future()
        playback.set_volume_state.return_value = result

        handle_web_player_commands(playback, ApplianceMenu(), 8, False)

        self.assertEqual(player_control.command_result(rejected.id), "rejected")
        self.assertEqual(player_control.command_result(superseded.id), "superseded")
        self.assertEqual(player_control.command_result(applied.id), "queued")
        playback.set_volume_state.assert_called_once_with(7, False)
        result.set_result(None)
        self.assertEqual(player_control.command_result(applied.id), "applied")

    def test_invalid_queue_entries_do_not_block_valid_commands(self) -> None:
        self.commands_path.write_text(
            json.dumps([None, {"action": None}, {"action": "volume", "value": 9, "created_at": "broken"}]),
            encoding="utf-8",
        )
        command, = consume_web_player_commands()

        self.assertEqual(command.action, "volume")
        self.assertEqual(command.value, 9)
        self.assertEqual(command.created_at, 0)

    def test_failed_pause_does_not_drop_the_following_volume_command(self):
        pause = queue_web_player_command("pause")
        volume = queue_web_player_command("volume", 7)
        playback = Mock(spec=PlaybackOwner)
        playback.pause.side_effect = OSError("could not save progress")
        playback.set_volume_state.return_value = None
        self.assertEqual(handle_web_player_commands(playback, ApplianceMenu(), 8, False),
                         (7, False, True, False))
        self.assertEqual(player_control.command_result(pause.id), "rejected")
        self.assertEqual(player_control.command_result(volume.id), "applied")
        playback.set_volume_state.assert_called_once_with(7, False)
        self.assertEqual(consume_web_player_commands(), [])

    def test_status_and_queue_keep_the_last_write_if_replacement_fails(self) -> None:
        original = queue_web_player_command("pause")
        save_web_player_status(WebPlayerStatus(title="The Hobbit", volume_percent=3))

        with patch.object(Path, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                queue_web_player_command("play")
            with self.assertRaises(OSError):
                save_web_player_status(WebPlayerStatus(title="Matilda"))

        self.assertEqual(consume_web_player_commands(), [original])
        self.assertEqual(load_web_player_status().title, "The Hobbit")
        self.assertEqual(load_web_player_status().volume_percent, 3)
        self.assertEqual(set(self.status_path.parent.iterdir()), {self.status_path, self.commands_path})

    def test_malformed_status_falls_back_to_safe_values(self) -> None:
        self.status_path.write_text('{"current_time": "broken", "volume_percent": Infinity}', encoding="utf-8")

        self.assertEqual(load_web_player_status(), WebPlayerStatus())

    def test_unreadable_queue_survives_a_failed_submit_and_is_consumed_once_on_recovery(self):
        original = queue_web_player_command("pause")
        consume = Mock(wraps=consume_web_player_commands)
        inbox = WebCommandInbox(consume)
        with patch.object(player_control.time, "monotonic", side_effect=[10, 11, 15, 16]):
            with patch.object(Path, "read_text", side_effect=PermissionError("temporarily unreadable")):
                with self.assertRaises(PermissionError):
                    queue_web_player_command("play")
                self.assertEqual(inbox.read(), [])
            self.assertEqual(inbox.read(), [])
            consume.assert_called_once_with()
            self.assertEqual(inbox.read(), [original])
            self.assertEqual(inbox.read(), [])
