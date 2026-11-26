"""Load the GStreamer library independently of any book, pipeline or audio device."""

from __future__ import annotations

import os
import threading
from typing import Any

from .gstreamer_preload import preload_audiobook_plugins
from .playback_timing import PlaybackTiming, measure_pipeline_setup, trace_gstreamer_init


class PlaybackError(RuntimeError):
    pass


def load_gstreamer(timing: PlaybackTiming | None = None):
    try:
        with measure_pipeline_setup(timing, "bindings"):
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
    except Exception as error:
        raise PlaybackError(f"GStreamer is unavailable: {error}") from error

    with trace_gstreamer_init(), measure_pipeline_setup(timing, "init"):
        Gst.init(None)
    return Gst


class GStreamerRuntime:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._module: Any | None = None
        self._stop_preloading = threading.Event()

    def load(self, timing: PlaybackTiming | None = None):
        self.stop_preloading()
        return self._initialise(timing)

    def stop_preloading(self) -> None:
        """Let the current native call finish and skip any remaining optional plugins."""
        self._stop_preloading.set()

    def _initialise(self, timing: PlaybackTiming | None):
        started_at = timing.clock() if timing is not None else None
        with self._lock:
            if timing is not None and started_at is not None:
                timing.pipeline_setup_seconds["runtime_wait"] = timing.clock() - started_at
            if self._module is None:
                self._module = load_gstreamer(timing)
            return self._module

    def start_warmup(self) -> None:
        def prepare() -> None:
            try:
                gst = self._initialise(None)
                if os.environ.get("CHAPTER_PRELOAD_AUDIO_PLUGINS") != "0":
                    preload_audiobook_plugins(gst, self._stop_preloading.is_set)
            except Exception as error:
                # A book selection retries through load() and reports playback errors normally.
                print(f"GStreamer warm-up failed: {error}")

        try:
            threading.Thread(target=prepare, name="chapter-gst-warmup", daemon=False).start()
        except RuntimeError as error:
            print(f"GStreamer warm-up could not start: {error}")
