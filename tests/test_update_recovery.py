from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from abs_kids_player.update_recovery import (
    OLED_SERVICE,
    setup_reports_fresh_display,
    wait_for_player_services,
)


class UpdateRecoveryTest(unittest.TestCase):
    def test_health_requires_the_expected_setup_version_and_recent_successful_oled_frame(self):
        for server, drawn_at, expected in (
            ("ChapterPlayer/0.4.0 Python/3.13", 99.0, True),
            ("ChapterPlayer/0.3.12 Python/3.13", 99.0, False),
            ("ChapterPlayer/0.4.0 Python/3.13", 93.0, False),
            ("ChapterPlayer/0.4.0 Python/3.13", 0.0, False),
        ):
            with self.subTest(server=server, drawn_at=drawn_at):
                response = MagicMock()
                response.headers = {"Server": server}
                response.read.return_value = json.dumps({"displayDrawnAt": drawn_at, "updatedAt": 100.0}).encode()
                with patch("abs_kids_player.update_recovery.build_opener") as opener, patch(
                    "abs_kids_player.update_recovery.time.time", return_value=100.0
                ):
                    opener().open().__enter__.return_value = response
                    self.assertEqual(setup_reports_fresh_display("0.4.0", 95.0), expected)

    def test_health_waits_for_stable_new_processes(self):
        elapsed = 0.0

        def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds

        def runner(args, **_kwargs):
            pid = 100 if args[-1] == OLED_SERVICE and elapsed < 4 else 101
            return subprocess.CompletedProcess(args, 0, f"ActiveState=active\nMainPID={pid}\nNRestarts=0\n")

        wait_for_player_services("0.4.0", 100.0, runner, clock=lambda: elapsed, sleep=sleep,
                                 reports_fresh_display=lambda *_: True)
        self.assertEqual(elapsed, 9)

    def test_health_explains_which_service_failed(self):
        def runner(args, **_kwargs):
            state = "failed" if args[-1] == OLED_SERVICE else "active"
            return subprocess.CompletedProcess(args, 0, f"ActiveState={state}\nMainPID=100\nNRestarts=0\n")

        with self.assertRaisesRegex(RuntimeError, OLED_SERVICE):
            wait_for_player_services("0.4.0", 100.0, runner, timeout=0)

    def test_boot_restores_checkout_and_service_files_without_touching_user_data(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from abs_kids_player.update_recovery import recover_interrupted_update, retain_deployment

        with TemporaryDirectory() as folder:
            root = Path(folder)
            service = root / "service"
            service.write_text("previous unit")
            new_unit = root / "new-unit"
            saved_book = root / "download.m4b"
            saved_book.write_text("book")
            git_dir = root / ".git"
            git_dir.mkdir()
            git_lock = git_dir / "index.lock"
            git_lock.touch()
            app = root / "chapter"
            app.mkdir()
            journal = retain_deployment(
                root, (service, new_unit), previous_commit="a" * 40, previous_version="0.3.12",
                target_version="0.4.0", install_dir=str(app), install_user="chapter",
                state_file=str(root / "software-update.json"),
            )
            service.write_text("new unit")
            new_unit.write_text("added service")
            commands = []

            def runner(args, **options):
                commands.append(args)
                if args[0] == "rm":
                    for path in args[3:]:
                        Path(path).unlink(missing_ok=True)
                if "checkout" in args:
                    self.assertFalse(git_lock.exists())
                return subprocess.CompletedProcess(args, 0, stdout=str(git_dir), stderr="")

            with patch("abs_kids_player.update_recovery.os.geteuid", return_value=1000), patch(
                "abs_kids_player.update_recovery.os.sync"
            ):
                self.assertTrue(recover_interrupted_update(root, runner))
                self.assertFalse(recover_interrupted_update(root, runner))
            self.assertEqual(service.read_text(), "previous unit")
            self.assertFalse(new_unit.exists())
            self.assertFalse(journal.exists())
            self.assertEqual(saved_book.read_text(), "book")
            self.assertIn(["git", "-C", str(app), "checkout", "--force", "--detach", "a" * 40], commands)
            self.assertIn(["systemctl", "daemon-reload"], commands)
            self.assertIn("Update interrupted", commands[-1][-1])
            self.assertFalse(any("restart" in args for args in commands))

    def test_install_process_leaves_no_boot_recovery_work_while_holding_lock(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock

        from abs_kids_player.update_recovery import recover_interrupted_update, recovery_lock

        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "pending").mkdir()
            runner = Mock()
            with recovery_lock(root) as acquired:
                self.assertTrue(acquired)
                self.assertFalse(recover_interrupted_update(root, runner))
            runner.assert_not_called()
            self.assertTrue((root / "pending").is_dir())
