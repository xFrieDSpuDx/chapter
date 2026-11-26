import subprocess
import sys
import threading
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from unittest.mock import patch

from abs_kids_player.bluetooth_commands import (
    CommandResult,
    run_bluetoothctl,
    run_bluetoothctl_with_agent,
)


class BluetoothCommandTest(unittest.TestCase):
    def test_leaving_pairing_reaps_the_active_command(self):
        spawned, cancel = threading.Event(), threading.Event()
        children = []
        popen = subprocess.Popen

        def start_child(args, **kwargs):
            self.assertEqual(args, ["bluetoothctl", "--agent", "NoInputNoOutput", "--timeout", "25", "pair", "speaker"])
            child = popen([sys.executable, "-c", "import time; time.sleep(5)"], **kwargs)
            children.append(child)
            spawned.set()
            return child

        with patch("abs_kids_player.bluetooth_commands.subprocess.Popen", side_effect=start_child), ThreadPoolExecutor(1) as worker:
            pairing = worker.submit(run_bluetoothctl_with_agent, ["pair", "speaker"], 25, cancel=cancel)
            try:
                self.assertTrue(spawned.wait(2))
                cancel.set()
                with self.assertRaises(CancelledError):
                    pairing.result(timeout=2)
                self.assertIsNotNone(children[0].returncode)
            finally:
                cancel.set()
                for child in children:
                    if child.poll() is None:
                        child.kill()

    def test_command_timeout_is_still_reported_as_a_failure(self):
        popen = subprocess.Popen
        with patch("abs_kids_player.bluetooth_commands.subprocess.Popen",
                   side_effect=lambda args, **kwargs: popen([sys.executable, "-c", "import time; time.sleep(5)"], **kwargs)):
            result = run_bluetoothctl(["show"], timeout=0)
        self.assertEqual(result, CommandResult(124, "bluetoothctl timed out"))

    def test_cancelling_before_launch_never_starts_bluetoothctl(self):
        cancel = threading.Event()
        cancel.set()
        with patch("abs_kids_player.bluetooth_commands.subprocess.Popen") as launch:
            with self.assertRaises(CancelledError):
                run_bluetoothctl(["power", "on"], cancel=cancel)
            launch.assert_not_called()
