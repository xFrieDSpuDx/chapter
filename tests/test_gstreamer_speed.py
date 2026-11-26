"""Exercise the Python binding and actual audio segments without a speaker or network."""

import os
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from abs_kids_player.playback import GStreamerPlayback


class NativeSpeedTest(unittest.TestCase):
    def missing_dependency(self, reason: str) -> None:
        if os.environ.get("CHAPTER_REQUIRE_GSTREAMER") == "1":
            self.fail(reason)
        self.skipTest(reason)

    def test_rate_changes_and_resume_reach_the_audio_filter(self):
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except (ImportError, ValueError):
            self.missing_dependency("GStreamer Python bindings unavailable")

        Gst.init(None)
        for name in ("playbin", "wavparse", "scaletempo", "fakesink"):
            if not Gst.ElementFactory.find(name):
                self.missing_dependency(f"GStreamer plugin {name} unavailable")

        wav = Path(self.enterContext(tempfile.TemporaryDirectory())) / "book.wav"
        with wave.open(str(wav), "wb") as output:
            output.setparams((1, 2, 8000, 0, "NONE", ""))
            output.writeframes(bytes(2 * 8000 * 30))
        engine = GStreamerPlayback()
        engine.Gst = Gst
        pipeline = engine.playbin = Gst.ElementFactory.make("playbin")
        self.addCleanup(pipeline.set_state, Gst.State.NULL)
        sink = Gst.ElementFactory.make("fakesink")
        sink.set_property("sync", True)
        pipeline.set_property("audio-sink", sink)
        engine.configure_tempo_filter()
        tempo = pipeline.get_property("audio-filter").get_static_pad("sink")
        pipeline.set_property("uri", wav.as_uri())
        engine.session = SimpleNamespace(duration=30, tracks=[SimpleNamespace(start_offset=0, duration=30)])
        pipeline.set_state(Gst.State.PLAYING)
        self.assertEqual(pipeline.get_state(5 * Gst.SECOND).state, Gst.State.PLAYING)
        engine.is_playing = True

        def check_rate(rate):
            self.assertEqual(pipeline.get_state(5 * Gst.SECOND).state, Gst.State.PLAYING)
            segment = tempo.get_sticky_event(Gst.EventType.SEGMENT, 0).parse_segment()
            self.assertEqual(segment.rate, rate)

        for rate in (1.25, 2.0, 1.75, 1.5, 1.0):
            engine.set_playback_rate(rate)
            check_rate(rate)
        for rate in (1.25, 1.75, 2.0):
            engine.pause(sync=False)
            engine.set_playback_rate(rate)
            self.assertFalse(engine.is_playing)
            engine.toggle()
            self.assertTrue(engine.is_playing)
            check_rate(rate)
