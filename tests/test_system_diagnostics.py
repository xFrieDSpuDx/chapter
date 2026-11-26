from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from abs_kids_player.build_info import installed_build, source_fingerprint
from abs_kids_player.catalogue_settings import report_catalogue_result
from abs_kids_player.config import AppConfig, PodcastConfig
from abs_kids_player.playback_startup_history import (
    SAMPLES_PER_SOURCE,
    StartupSample,
    load_startup_history,
    record_startup_sample,
    startup_history_path,
)
from abs_kids_player.playback_timing import PlaybackTiming
from abs_kids_player.progress_sync import ProgressSync, progress_account
from abs_kids_player.setup_server import SetupHandler
from abs_kids_player.system_diagnostics import collect_system_diagnostics


class SystemDiagnosticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict("os.environ", {
            "ABS_KIDS_PLAYER_STATE_PATH": str(self.directory / "last-playback.json"),
            "ABS_KIDS_PLAYER_LIBRARY_CACHE_PATH": str(self.directory / "library-cache.json"),
        }))
        self.config = AppConfig(
            server_url="https://private-audiobooks.example.test",
            token="private-token",
            refresh_token="private-refresh-token",
            username="private-account",
            library_id="private-library",
            podcasts=[PodcastConfig(url="https://private-podcast.example.test")],
        )

    def test_build_id_is_stable_across_directories_and_changes_with_shipped_code(self) -> None:
        first, second = self.directory / "first", self.directory / "second"
        first.mkdir()
        second.mkdir()
        content = "print('Chapter')"
        (first / "player.py").write_text(content)
        (second / "player.py").write_text(content)
        (second / "player.pyc").write_bytes(b"old bytecode")
        self.assertEqual(source_fingerprint(first), source_fingerprint(second))
        self.assertRegex(source_fingerprint(first), r"^[0-9a-f]{12}$")
        (second / "player.py").write_text(content + "\n")
        self.assertNotEqual(source_fingerprint(first), source_fingerprint(second))

    def test_existing_playback_timing_records_first_audio_and_only_known_phases(self) -> None:
        timing = PlaybackTiming(
            1.0, 1.1, "continue", source="download", prepared_at=1.2, pipeline_at=1.3,
            preroll_at=1.5, preroll_finished_at=1.87, startup_finished_at=2.1,
            pipeline_setup_seconds={"bindings": 0.01, "init": 0.03, "runtime_wait": 0.04, "private-file": 2},
            startup_reporter=record_startup_sample, first_pipeline_since_restart=True,
        )
        with patch("builtins.print"):
            timing.observe_output("output_observed", rendered_at=2.23)
            timing.report()
        row, = load_startup_history()
        self.assertEqual((row.source, row.action, row.outcome), ("download", "continue", "output_observed"))
        self.assertEqual((row.durations_ms["select_to_output_ms"], row.durations_ms["gst_init_ms"]), (1230, 30))
        self.assertEqual(row.durations_ms["gst_runtime_wait_ms"], 40)
        self.assertNotIn("gst_private-file_ms", row.durations_ms)
        self.assertEqual(row.build_id, installed_build().build_id)
        self.assertIs(row.first_pipeline_since_restart, True)

    def test_normal_listening_retains_only_the_latest_measured_first_play_beyond_recent_history(self) -> None:
        for when in (100, 200):
            record_startup_sample(StartupSample(
                when, "download", "continue", "output_observed", {}, first_pipeline_since_restart=True,
            ))
        for index in range(SAMPLES_PER_SOURCE + 1):
            record_startup_sample(StartupSample(
                300 + index, "download", "continue", "output_observed", {}, first_pipeline_since_restart=False,
            ))
        record_startup_sample(StartupSample(
            400, "download", "start", "gstreamer_failed", {}, first_pipeline_since_restart=True,
        ))
        history = load_startup_history()
        self.assertEqual(len(history), SAMPLES_PER_SOURCE + 1)
        self.assertEqual(history[-1].recorded_at, 200)
        self.assertIs(history[1].first_pipeline_since_restart, False)

    def test_new_downloads_do_not_displace_recent_streaming_timings(self) -> None:
        record_startup_sample(StartupSample(100, "audiobookshelf", "start", "output_observed", {}))
        for index in range(SAMPLES_PER_SOURCE + 2):
            record_startup_sample(StartupSample(200 + index, "download", "start", "output_observed", {}))
        history = load_startup_history()
        self.assertEqual(len([row for row in history if row.source == "download"]), SAMPLES_PER_SOURCE)
        self.assertEqual(history[-1].source, "audiobookshelf")
        self.assertEqual(history[0].recorded_at, 200 + SAMPLES_PER_SOURCE + 1)

    def test_an_unwritable_history_does_not_interrupt_playback_reporting(self) -> None:
        timing = PlaybackTiming(1.0, 1.1, "start", source="audiobookshelf", startup_finished_at=1.6,
                                startup_reporter=record_startup_sample)
        with (
            patch("abs_kids_player.playback_startup_history.write_json_atomically", side_effect=PermissionError),
            patch("builtins.print"),
        ):
            timing.observe_output("output_observed", rendered_at=1.7)
        self.assertEqual(timing.status, "output_observed")
        self.assertEqual(load_startup_history(), [])

    def get(self, path):
        handler = object.__new__(SetupHandler)
        handler.path = path
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        with patch("abs_kids_player.setup_server.load_config", return_value=self.config):
            handler.do_GET()
        return handler

    def test_diagnostic_export_passes_only_allowlisted_data(self) -> None:
        report_catalogue_result(self.config, "private-token from private-server error")
        queue = ProgressSync()
        queue.record(progress_account(self.config), "private-book-id", 42, 60, session_id="private-session")
        queue.record("previous-account", "another-private-id", 17, 60)
        startup_history_path().write_text(json.dumps([{
            "recorded_at": 100, "source": "audiobookshelf", "action": "continue",
            "outcome": "output_observed", "output": "private-bluetooth-device",
            "durations_ms": {"select_to_output_ms": 2345, "url": "private-url"},
            "track_id": "private-track-id", "request_range": "private-range",
            "build_id": "private-file-path",
        }]))

        response = self.get("/system/diagnostics")
        response.send_response.assert_called_once_with(200)
        response.send_header.assert_any_call("Content-Disposition", 'attachment; filename="chapter-diagnostics.json"')
        response.send_header.assert_any_call("Cache-Control", "no-store")
        body = response.wfile.getvalue().decode()
        self.assertNotIn("private-", body)
        self.assertNotIn("http", body)
        data = json.loads(body)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["pending_progress_count"], 1)
        self.assertEqual(data["library_refresh_result"], "failed")
        self.assertEqual(data["playback_startups"][0]["durations_ms"], {"select_to_output_ms": 2345})
        self.assertIsNone(data["playback_startups"][0]["output"])
        self.assertIsNone(data["playback_startups"][0]["build_id"])
        self.assertIsNone(data["playback_startups"][0]["first_pipeline_since_restart"])

    def test_status_page_displays_both_sources_and_distinguishes_unobserved_audio(self) -> None:
        record_startup_sample(StartupSample(
            100, "audiobookshelf", "start", "sink_stats_unavailable", {"prepare_ms": 250},
            first_pipeline_since_restart=True,
        ))
        record_startup_sample(StartupSample(
            200, "download", "continue", "output_observed", {"select_to_output_ms": 1230},
            first_pipeline_since_restart=False,
        ))
        response = self.get("/system/status")
        response.send_response.assert_called_once_with(200)
        page = response.wfile.getvalue().decode()
        self.assertIn('class="text-action back-link" href="/?tab=system">System</a>', page)
        for item in ("System status", "Downloaded", "Audiobookshelf stream", "1.23 s",
                     "Audio timing unavailable · First play after restart", "Audio observed · Later play",
                     "Not recorded yet", "Download diagnostics (JSON)"):
            self.assertIn(item, page)

    def test_storage_failure_leaves_other_diagnostics_available(self) -> None:
        with patch("abs_kids_player.system_diagnostics.storage_usage", side_effect=OSError):
            report = collect_system_diagnostics(self.config)
        self.assertIsNone(report.storage.free_bytes)
        self.assertIsNone(report.storage.download_bytes)
        self.assertEqual(report.build.version, installed_build().version)
