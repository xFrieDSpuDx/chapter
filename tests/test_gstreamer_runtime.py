from __future__ import annotations

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from abs_kids_player.gstreamer_runtime import GStreamerRuntime, PlaybackError
from abs_kids_player.playback_timing import PlaybackTiming


class GStreamerRuntimeTest(unittest.TestCase):
    def test_selection_during_plugin_load_skips_remaining_optional_work(self):
        runtime = GStreamerRuntime()
        loading, release, yielded = threading.Event(), threading.Event(), threading.Event()
        gst = Mock()
        reader = gst.ElementFactory.find.return_value

        def load_reader():
            loading.set()
            if not release.wait(3):
                raise TimeoutError("Reader was not released")
            return reader

        def watch_log(message):
            if message == "GStreamer preload role=aac_decoder status=yielded":
                yielded.set()

        reader.load.side_effect = load_reader
        with (
            patch.dict(os.environ, CHAPTER_PRELOAD_AUDIO_PLUGINS="1"),
            patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=gst),
            patch("builtins.print", side_effect=watch_log),
            ThreadPoolExecutor(max_workers=1) as worker,
        ):
            runtime.start_warmup()
            try:
                self.assertTrue(loading.wait(2))
                self.assertIs(worker.submit(runtime.load).result(timeout=1), gst)
            finally:
                release.set()
            self.assertTrue(yielded.wait(2))
        gst.ElementFactory.list_get_elements.assert_not_called()

    def test_control_run_still_initialises_core_without_loading_optional_plugins(self):
        gst = Mock()
        runtime = GStreamerRuntime()
        # Run the thread target synchronously so the assertions cover its whole lifetime.
        with (
            patch.dict(os.environ, CHAPTER_PRELOAD_AUDIO_PLUGINS="0"),
            patch("abs_kids_player.gstreamer_runtime.load_gstreamer", return_value=gst) as initialise,
            patch("abs_kids_player.gstreamer_runtime.threading.Thread") as thread,
        ):
            runtime.start_warmup()
            thread.call_args.kwargs["target"]()
            self.assertIs(runtime.load(), gst)
        initialise.assert_called_once_with(None)
        self.assertEqual(gst.mock_calls, [])

    def test_shutdown_during_initialisation_skips_optional_plugins(self):
        runtime = GStreamerRuntime()
        loading, release = threading.Event(), threading.Event()
        warmup_threads = []
        gst = Mock()
        def initialise(_timing):
            warmup_threads.append(threading.current_thread())
            loading.set()
            if not release.wait(2):
                raise TimeoutError("Initialisation was not released")
            return gst

        with (patch.dict(os.environ, CHAPTER_PRELOAD_AUDIO_PLUGINS="1"),
              patch("abs_kids_player.gstreamer_runtime.load_gstreamer", side_effect=initialise)):
            runtime.start_warmup()
            try:
                self.assertTrue(loading.wait(1))
                runtime.stop_preloading()
            finally:
                release.set()
            warmup, = warmup_threads
            warmup.join(timeout=2)
            self.assertFalse(warmup.is_alive())
        gst.ElementFactory.find.assert_not_called()
        gst.ElementFactory.list_get_elements.assert_not_called()

    def test_immediate_selection_shares_the_background_load_and_reports_its_wait(self):
        runtime = GStreamerRuntime()
        loading, selecting, release = threading.Event(), threading.Event(), threading.Event()
        module = Mock()

        def initialise(_timing):
            loading.set()
            if not release.wait(3):
                raise TimeoutError("Warm-up was not released")
            return module

        def clock():
            now = 11 if release.is_set() else 10
            selecting.set()
            return now

        timing = PlaybackTiming(10, 10, "continue", clock=clock)
        with patch("abs_kids_player.gstreamer_runtime.load_gstreamer", side_effect=initialise) as loader:
            runtime.start_warmup()
            with ThreadPoolExecutor(max_workers=1) as worker:
                try:
                    self.assertTrue(loading.wait(2))
                    selected = worker.submit(runtime.load, timing)
                    self.assertTrue(selecting.wait(2))
                    self.assertFalse(selected.done())
                finally:
                    release.set()
                self.assertIs(selected.result(timeout=2), module)
            self.assertIs(runtime.load(), module)
            loader.assert_called_once_with(None)
            self.assertEqual(timing.pipeline_setup_seconds["runtime_wait"], 1)
            self.assertEqual(module.mock_calls, [])

    def test_failed_warmup_allows_playback_to_retry(self):
        runtime = GStreamerRuntime()
        reported = threading.Event()
        module = Mock()
        with (
            patch("abs_kids_player.gstreamer_runtime.load_gstreamer",
                  side_effect=[PlaybackError("unavailable"), module]) as loader,
            patch("builtins.print", side_effect=lambda *args: reported.set()),
        ):
            runtime.start_warmup()
            self.assertTrue(reported.wait(2))
            self.assertIs(runtime.load(), module)
            self.assertIs(runtime.load(), module)
            self.assertEqual(loader.call_count, 2)
