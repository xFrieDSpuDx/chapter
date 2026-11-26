"""Check persisted downloads across process restarts and both directions of a build switch."""

import os
import tempfile
import unittest
from pathlib import Path

from check_download_reboot import ROOT, check_reboot, run_phase


class DownloadRetentionTest(unittest.TestCase):
    def test_interrupted_download_resumes_and_both_books_open_after_rollback(self):
        previous = Path(os.environ.get("CHAPTER_TEST_PREVIOUS_BUILD", ROOT)).resolve()
        candidate = Path(os.environ.get("CHAPTER_TEST_CANDIDATE_BUILD", ROOT)).resolve()
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / ".chapter-test-home").touch()
            run_phase(home, previous, "seed")
            run_phase(home, candidate, "offline")
            run_phase(home, candidate, "resume")
            run_phase(home, candidate, "verify")
            run_phase(home, previous, "verify")

    def test_reboot_check_requires_a_changed_boot_and_can_be_verified_twice(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "test-home"
            with self.assertRaisesRegex(ValueError, "Run 'before'"):
                check_reboot(home, "after", "boot-one")
            check_reboot(home, "before", "boot-one")
            with self.assertRaises(FileExistsError):
                check_reboot(home, "before", "boot-one")
            with self.assertRaisesRegex(ValueError, "same boot"):
                check_reboot(home, "after", "boot-one")
            for _ in range(2):
                self.assertIn("Passed", check_reboot(home, "after", "boot-two"))
