import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from abs_kids_player.appliance_io import InputEvent
from abs_kids_player.dial_timing import DialTiming
from abs_kids_player.display_frames import SCREEN_OFF, VolumeFrame
from scripts.local_release.pi_checks import OwnerPage, dashboard


class DialTimingTest(unittest.TestCase):
    def test_reports_the_oldest_accepted_turn_through_the_completed_write(self):
        probe = DialTiming()
        probe.observe(InputEvent("volume", 1, 100.0))
        probe.observe(InputEvent("volume", 1, 100.01))
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "dial-timing"
            marker.touch()
            with (patch("abs_kids_player.dial_timing.CAPTURE_MARKER", marker),
                  patch("abs_kids_player.dial_timing.time.monotonic", side_effect=[100.02, 100.05]),
                  redirect_stdout(output)):
                draw = Mock(return_value=True)
                probe.show(VolumeFrame(10), draw)
                probe.show(VolumeFrame(10), draw)
        self.assertEqual(draw.call_count, 2)
        self.assertEqual(output.getvalue().strip(),
                         "Dial timing dial=volume turns=2 frame=VolumeFrame to_draw_ms=20.0 draw_ms=30.0 to_frame_ms=50.0")

    def test_off_duplicate_missing_and_expired_capture_emit_no_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            probe = DialTiming()
            output = io.StringIO()
            with patch("abs_kids_player.dial_timing.CAPTURE_MARKER", marker), redirect_stdout(output):
                for frame, written in [(VolumeFrame(10), True), (SCREEN_OFF, False), (VolumeFrame(10), False)]:
                    probe.observe(InputEvent("nav", 1))
                    probe.show(frame, lambda _: written)
                    marker.touch()
                with patch("abs_kids_player.dial_timing.time.time", return_value=marker.stat().st_mtime + 91):
                    probe.observe(InputEvent("nav", 1))
                    probe.show(VolumeFrame(10), lambda _: True)
            self.assertEqual(output.getvalue(), "")


class DashboardCheckTest(unittest.TestCase):
    def test_protected_overview_times_the_owner_page_and_closes_its_own_session(self):
        guest = OwnerPage(b"<div><h1>Overview</h1></div>")
        login = OwnerPage(b'<input name="csrf_token" value="form">')
        owner = OwnerPage(b'<div data-dashboard></div><input name="csrf_token" value="form">')
        replies = [(guest, 1, 2), (login, 1, 2), (owner, 200, 800)] + [(owner, 10, 12)] * 7 + [(login, 1, 2)]
        output = io.StringIO()
        with (patch("scripts.local_release.pi_checks.request", side_effect=replies) as http,
              patch("builtins.input", return_value="reader"), patch("scripts.local_release.pi_checks.getpass", return_value="pw"),
              patch("scripts.local_release.pi_checks.time.sleep"), redirect_stdout(output)):
            dashboard()
        self.assertEqual(http.call_args_list[-1].args[1:], ("/owner/sign-out", {"csrf_token": "form"}))
        self.assertIn("median=12.0 ms", output.getvalue())
        self.assertNotIn("pw", output.getvalue())
