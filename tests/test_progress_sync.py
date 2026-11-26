from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from abs_kids_player.api import AudiobookshelfError
from abs_kids_player.config import AppConfig
from abs_kids_player.player_state import state_path
from abs_kids_player.progress_sync import (
    ProgressSync,
    SessionProgress,
    load_progress_sync_status,
    progress_account,
    read_listening_progress,
    read_pending_progress,
)


class ProgressSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {"ABS_KIDS_PLAYER_STATE_DIR": directory}))
        self.account = AppConfig(server_url="https://books.example.com", username="listener", token="token")
        self.key = progress_account(self.account)
        self.client = Mock()
        self.enterContext(patch("abs_kids_player.api.client_from_config", return_value=self.client))
        self.thread = self.enterContext(patch("abs_kids_player.progress_sync.threading.Thread"))

    def run_worker(self, sync: ProgressSync, config: AppConfig | None = None) -> None:
        previous_workers = self.thread.call_count
        sync.pump(config or self.account)
        self.assertEqual(self.thread.call_count, previous_workers + 1, "Expected a new progress worker")
        details = self.thread.call_args.kwargs
        details["target"](*details["args"])

    def test_rejected_book_waits_while_another_syncs_then_sends_its_latest_position(self) -> None:
        clock = self.enterContext(patch("abs_kids_player.progress_sync.time.monotonic", return_value=100))
        sync = ProgressSync(retry_seconds=15)
        sync.record(self.key, "first", 50, 400)
        sync.record(self.key, "second", 25, 400)
        self.client.get_progress.return_value = {}
        self.client.update_progress.side_effect = [AudiobookshelfError("book unavailable"), None, None]

        self.run_worker(sync)
        sync.record(self.key, "first", 12, 400)  # The listener rewinds during the retry delay.
        self.run_worker(sync)
        pending = read_pending_progress()
        self.assertEqual({row.item_id: row.current_time for row in pending.values()}, {"first": 12})
        self.assertEqual(read_listening_progress()[(self.key, "second", "")].current_time, 25)

        clock.return_value = 114
        sync.pump(self.account)
        self.assertEqual(self.thread.call_count, 2)
        clock.return_value = 115
        self.run_worker(sync)
        attempts = [call.args[:2] for call in self.client.update_progress.call_args_list]
        self.assertEqual(attempts, [("first", 50), ("second", 25), ("first", 12)])
        self.assertFalse(read_pending_progress())

    def test_rejected_book_gives_the_next_book_a_turn_even_after_the_retry_delay(self) -> None:
        clock = self.enterContext(patch("abs_kids_player.progress_sync.time.monotonic", return_value=100))
        sync = ProgressSync(retry_seconds=15)
        sync.record(self.key, "unavailable", 10, 400)
        sync.record(self.key, "available", 20, 400)
        self.client.get_progress.return_value = {}
        self.client.update_progress.side_effect = [AudiobookshelfError("book unavailable"), None]

        self.run_worker(sync)
        clock.return_value = 120
        self.run_worker(sync)

        self.assertEqual(self.client.update_progress.call_args.args[0], "available")
        self.assertEqual(set(read_pending_progress()), {(self.key, "unavailable", "")})

    def test_episode_retry_is_scoped_to_its_episode_and_account(self) -> None:
        self.enterContext(patch("abs_kids_player.progress_sync.time.monotonic", return_value=100))
        other = replace(self.account, username="another listener")
        sync = ProgressSync(retry_seconds=15)
        sync.record(self.key, "show", 10, 400, episode_id="a")
        sync.record(self.key, "show", 20, 400, episode_id="b")
        sync.record(progress_account(other), "show", 30, 400, episode_id="a")
        self.client.get_progress.side_effect = [AudiobookshelfError("episode unavailable"), {}, {}]

        self.run_worker(sync)
        self.run_worker(sync, other)
        self.run_worker(sync)
        sync.pump(self.account)

        self.assertEqual(self.thread.call_count, 3)
        uploads = [(call.kwargs["episode_id"], call.args[1]) for call in self.client.update_progress.call_args_list]
        self.assertEqual(uploads, [("a", 30), ("b", 20)])
        self.assertEqual(set(read_pending_progress()), {(self.key, "show", "a")})

    def test_worker_launch_failure_retries_and_sends_the_saved_position(self):
        sync = ProgressSync(retry_seconds=10)
        sync.record(self.key, "book", 18, 60)
        self.thread.return_value.start.side_effect = [RuntimeError("no worker"), None]
        with patch("abs_kids_player.progress_sync.time.monotonic", return_value=100):
            sync.pump(self.account)
            sync.pump(self.account)
        self.assertEqual(self.thread.call_count, 1)
        self.assertTrue(read_pending_progress())
        self.client.get_progress.return_value = {}
        with patch("abs_kids_player.progress_sync.time.monotonic", return_value=111):
            self.run_worker(sync)
        self.client.update_progress.assert_called_once_with(
            "book", 18, 60, episode_id="", session_id="", time_listened=0,
        )
        self.assertFalse(read_pending_progress())

    def test_outage_and_restart_preserve_a_rewind_to_zero(self) -> None:
        sync = ProgressSync(retry_seconds=0)
        sync.record(self.key, "book", 100, 400)
        self.client.get_progress.side_effect = AudiobookshelfError("offline")
        self.run_worker(sync)
        sync.record(self.key, "book", 0, 400)

        self.client.get_progress.side_effect = None
        self.client.get_progress.return_value = {"currentTime": 25, "lastUpdate": 1}
        self.run_worker(ProgressSync())

        self.client.update_progress.assert_called_once_with(
            "book", 0, 400, episode_id="", session_id="", time_listened=0
        )
        self.assertFalse(read_pending_progress())

    def test_superseded_worker_never_writes_an_earlier_position(self) -> None:
        sync = ProgressSync()
        sync.record(self.key, "book", 100, 400)
        sync.pump(self.account)
        details = self.thread.call_args.kwargs
        sync.record(self.key, "book", 12, 400)
        self.client.get_progress.return_value = {}

        details["target"](*details["args"])
        self.client.update_progress.assert_not_called()
        self.run_worker(sync)
        self.assertEqual(self.client.update_progress.call_args.args, ("book", 12, 400))

    def test_only_flushes_positions_for_the_current_account_and_episode(self) -> None:
        other = replace(self.account, username="someone else")
        sync = ProgressSync()
        sync.record(self.key, "podcast", 10, 400, "episode-a")
        sync.record(self.key, "podcast", 20, 400, "episode-b")
        sync.pump(other)
        self.thread.assert_not_called()

        self.client.get_progress.return_value = {}
        self.run_worker(sync)
        self.assertEqual(self.client.update_progress.call_args.kwargs["episode_id"], "episode-a")
        self.assertIn((self.key, "podcast", "episode-b"), read_pending_progress())
        self.assertEqual(progress_account(replace(self.account, token="refreshed")), self.key)

    def test_a_newer_position_saved_on_another_device_wins(self) -> None:
        sync = ProgressSync()
        sync.record(self.key, "book", 12, 400)
        self.client.get_progress.return_value = {"currentTime": 110, "lastUpdate": 9_999_999_999_999}
        self.run_worker(sync)
        self.client.update_progress.assert_not_called()
        self.assertFalse(read_pending_progress())
        self.assertEqual(read_listening_progress()[(self.key, "book", "")].current_time, 110)
        status = load_progress_sync_status(self.key)
        self.assertEqual((status.outcome, status.last_success_at), ("server_newer", 0))

    def test_browser_takes_over_an_open_session_and_later_pi_ticks_cannot_move_it_back(self) -> None:
        sync = ProgressSync()
        session = SessionProgress((40, 100))
        self.client.get_progress.return_value = {"currentTime": 40, "lastUpdate": 100}
        sync.record(self.key, "book", 50, 400, session=session)
        self.run_worker(sync)
        # The server assigns a fresh timestamp to Chapter's own write.
        self.client.get_progress.return_value = {"currentTime": 50, "lastUpdate": 200}
        sync.record(self.key, "book", 55, 400, session=session)
        self.run_worker(sync)
        self.client.update_progress.reset_mock()

        self.client.get_progress.return_value = {"currentTime": 12, "lastUpdate": 300}
        sync.record(self.key, "book", 65, 400, session=session)
        self.run_worker(sync)
        self.assertEqual(session.taken_over, (12, 300))
        sync.record(self.key, "book", 70, 400, session=session)
        self.assertFalse(read_pending_progress())
        self.assertEqual(read_listening_progress()[(self.key, "book", "")].current_time, 12)
        self.client.update_progress.assert_not_called()

        sync.record(self.key, "book", 17, 400, session=SessionProgress((12, 300)))
        self.run_worker(sync)
        self.assertEqual(self.client.update_progress.call_args.args, ("book", 17, 400))

    def test_offline_reopen_yields_to_browser_edit_that_predates_the_stale_start(self) -> None:
        sync = ProgressSync()
        session = SessionProgress((5, 100), offline_opened_at=300)
        self.client.get_progress.return_value = {"currentTime": 72, "lastUpdate": 200}
        sync.record(self.key, "book", 9, 400, session=session)
        self.run_worker(sync)
        self.client.update_progress.assert_not_called()
        self.assertEqual(session.taken_over, (72, 200))

    def test_offline_reopen_preserves_newer_listening_when_both_players_edited_offline(self) -> None:
        sync = ProgressSync()
        session = SessionProgress((5, 100), offline_opened_at=200)
        self.client.get_progress.return_value = {"currentTime": 72, "lastUpdate": 300}
        sync.record(self.key, "book", 14, 400, session=session)
        self.run_worker(sync)
        self.assertEqual(self.client.update_progress.call_args.args, ("book", 14, 400))
        self.assertIsNone(session.taken_over)

    def test_sync_status_uses_server_acknowledgement_and_keeps_last_success_during_outage(self) -> None:
        sync = ProgressSync(retry_seconds=0)
        sync.record(self.key, "book", 100, 400)
        self.client.get_progress.return_value = {}
        with patch("abs_kids_player.progress_sync.time.time", return_value=1_000):
            self.run_worker(sync)
        self.assertEqual(load_progress_sync_status(self.key).last_success_at, 1_000)

        sync.record(self.key, "book", 120, 400)
        self.client.get_progress.side_effect = AudiobookshelfError("offline")
        with patch("abs_kids_player.progress_sync.time.time", return_value=2_000):
            self.run_worker(sync)

        status = load_progress_sync_status(self.key)
        self.assertEqual((status.last_success_at, status.last_attempt_at, status.outcome), (1_000, 2_000, "retrying"))
        self.assertEqual(load_progress_sync_status("another-account").last_success_at, 0)

    def test_restart_between_acknowledgement_and_queue_cleanup_keeps_newest_position(self) -> None:
        sync = ProgressSync()
        sync.record(self.key, "book", 100, 400)
        pending_file = state_path().with_name("pending-progress.json")
        pending_at_crash = pending_file.read_bytes()
        self.client.get_progress.return_value = {"currentTime": 7, "lastUpdate": 9_999_999_999_999}
        self.run_worker(sync)
        # The acknowledgement reached disk but the old queue survived the interruption.
        pending_file.write_bytes(pending_at_crash)
        self.assertEqual(read_listening_progress()[(self.key, "book", "")].current_time, 7)
        self.assertFalse(read_pending_progress())
        self.thread.reset_mock()
        ProgressSync().pump(self.account)
        self.thread.assert_not_called()

    def test_restart_after_success_does_not_send_the_acknowledged_edit_again(self) -> None:
        sync = ProgressSync()
        sync.record(self.key, "book", 25, 400)
        pending_file = state_path().with_name("pending-progress.json")
        pending_at_crash = pending_file.read_bytes()
        self.client.get_progress.return_value = {}
        self.run_worker(sync)
        pending_file.write_bytes(pending_at_crash)
        self.assertFalse(read_pending_progress())
        # A later, deliberate rewind must still be visible alongside the old acknowledgement.
        ProgressSync().record(self.key, "book", 0, 400)
        self.assertEqual(read_pending_progress()[(self.key, "book", "")].current_time, 0)

    def test_completed_old_write_cannot_discard_a_rewind_queued_during_it(self) -> None:
        sync = ProgressSync()
        sync.record(self.key, "book", 100, 400)
        self.client.get_progress.side_effect = [
            {}, {"currentTime": 100, "lastUpdate": 9_999_999_999_999},
        ]

        def rewind_during_first_send(*args, **kwargs) -> None:
            self.client.update_progress.side_effect = None
            sync.record(self.key, "book", 15, 400)

        self.client.update_progress.side_effect = rewind_during_first_send
        self.run_worker(sync)
        self.run_worker(sync)

        self.assertEqual([call.args[1] for call in self.client.update_progress.call_args_list], [100, 15])
        self.assertFalse(read_pending_progress())
