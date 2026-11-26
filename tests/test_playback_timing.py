from __future__ import annotations

import unittest
from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from abs_kids_player.playback_timing import (
    AudioRenderProbe,
    PlaybackTiming,
    find_audio_sink,
    measure_pipeline_setup,
    trace_gstreamer_init,
)
from abs_kids_player.preroll_probe import PrerollMilestones
from abs_kids_player.source_read_probe import SourceBuffer, SourceReadTrace


class AudioSink:
    def __init__(self, count: int) -> None:
        self.count = count

    def get_property(self, name: str) -> object:
        if name != "stats":
            raise ValueError(name)
        return SimpleNamespace(get_value=lambda _key: self.count)


class PlaybackTimingTest(unittest.TestCase):
    def test_output_timing_waits_for_a_new_render_at_startup_gain(self) -> None:
        clock = Mock(return_value=12.0)
        timing = PlaybackTiming(
            selected_at=10.0,
            requested_at=10.1,
            action="continue",
            clock=clock,
            source="audiobookshelf",
            source_seconds=0.25,
            prepared_at=10.4,
            pipeline_at=10.5,
        )
        sink = AudioSink(count=5)
        probe = AudioRenderProbe(timing, sink)
        with patch("builtins.print") as printer:
            probe.gain_applied()
            probe.poll()
            self.assertIsNone(timing.rendered_at)
            clock.return_value = 12.04
            sink.count += 1
            probe.poll()
            printer.assert_not_called()
            timing.startup_finished_at = 12.4
            timing.report()
            timing.report()

        printer.assert_called_once()
        line = printer.call_args.args[0]
        self.assertTrue(line.startswith("Playback timing status=output_observed "))
        for metric in (
            "input_ms=100",
            "audiobookshelf_ms=250",
            "owner_wait_ms=100",
            "gst_to_gain_ms=1500",
            "gst_ms=1900",
            "gst_to_output_ms=1540",
            "select_to_output_ms=2040",
        ):
            self.assertIn(metric, line)

    def test_missing_sink_counter_reports_gain_and_omits_output_duration(self) -> None:
        timing = PlaybackTiming(1.0, 1.0, "start", clock=lambda: 2.5, pipeline_at=1.5)
        AudioRenderProbe(timing, sink=None).gain_applied()

        with patch("builtins.print") as printer:
            timing.startup_finished_at = 2.9
            timing.report()

        line = printer.call_args.args[0]
        self.assertIn("status=sink_stats_unavailable", line)
        self.assertIn("select_to_gain_ms=1500", line)
        self.assertNotIn("select_to_output_ms=", line)

    def test_render_timeout_does_not_report_an_output_duration(self) -> None:
        clock = Mock(return_value=2.0)
        timing = PlaybackTiming(1.0, 1.0, "start", clock=clock)
        probe = AudioRenderProbe(timing, sink=AudioSink(count=0))
        probe.gain_applied()
        clock.return_value = 4.1
        probe.poll()

        self.assertEqual(timing.status, "output_timeout")
        self.assertIsNone(timing.rendered_at)

    def test_reports_pre_preroll_breakdown_and_gaps_between_waits(self) -> None:
        timing = PlaybackTiming(
            8.0,
            9.0,
            "continue",
            pipeline_at=10.0,
            pipeline_setup_seconds={"bindings": 0.25, "init": 5.5, "media_url": 0.3},
            preroll_at=16.25,
            preroll_finished_at=18.0,
            seek_at=19.0,
            seek_finished_at=20.0,
            playing_at=20.1,
            startup_finished_at=21.0,
        )
        with patch("builtins.print") as printer:
            timing.observe_output("output_observed", rendered_at=20.8)

        fields = printer.call_args.args[0].split()
        for field in (
            "gst_pre_preroll_ms=6250",
            "gst_pre_preroll_other_ms=200",
            "gst_bindings_ms=250",
            "gst_init_ms=5500",
            "gst_media_url_ms=300",
            "gst_preroll_to_seek_ms=1000",
            "gst_seek_to_play_ms=100",
        ):
            self.assertIn(field, fields)

    def test_early_seek_marks_where_initial_preroll_ended(self) -> None:
        timing = PlaybackTiming(
            1.0,
            1.1,
            "continue",
            preroll_at=2.0,
            pause_returned_at=2.05,
            preroll_finished_at=2.4,
            preroll_wait="seek_started",
            seek_at=2.4,
            seek_finished_at=2.6,
            seek_wait="completed",
            seek_mode="early",
            early_seek_attempt_at=2.4,
            startup_finished_at=3.0,
        )
        with patch("builtins.print") as printer:
            timing.observe_output("output_observed", rendered_at=2.85)

        fields = printer.call_args.args[0].split()
        for metric in (
            "gst_preroll_wait=seek_started",
            "gst_preroll_ms=400",
            "gst_early_seek_attempt_ms=400",
            "gst_seek_mode=early",
            "gst_seek_ms=200",
        ):
            self.assertIn(metric, fields)

    def test_gstreamer_init_trace_is_opt_in(self) -> None:
        trace = StringIO()
        with (
            patch.dict("os.environ", {"CHAPTER_TRACE_GST_INIT": ""}),
            redirect_stderr(trace),
            trace_gstreamer_init(),
        ):
            print("native registry trace", file=trace)

        self.assertEqual(trace.getvalue().splitlines(), ["native registry trace"])

    def test_reports_elapsed_setup_step_when_startup_fails_before_preroll(self) -> None:
        clock = Mock(side_effect=[10.0, 10.75])
        timing = PlaybackTiming(8.0, 9.0, "continue", clock=clock, pipeline_at=10.0)
        trace = StringIO()
        with (
            patch.dict("os.environ", {"CHAPTER_TRACE_GST_INIT": "1"}),
            patch("abs_kids_player.playback_timing.os.getpid", return_value=4321),
            redirect_stderr(trace),
            self.assertRaisesRegex(RuntimeError, "init failed"),
        ):
            with trace_gstreamer_init(), measure_pipeline_setup(timing, "init"):
                raise RuntimeError("init failed")

        self.assertEqual(
            trace.getvalue().splitlines(),
            [
                "GStreamer init phase=begin pid=4321",
                "GStreamer init phase=end pid=4321 status=failed",
            ],
        )
        with patch("builtins.print") as printer:
            timing.report_startup_failure()
        fields = printer.call_args.args[0].split()
        self.assertIn("status=gstreamer_failed", fields)
        self.assertIn("gst_init_ms=750", fields)
        self.assertFalse(any(field.startswith("gst_pre_preroll_") for field in fields))

    def test_logs_preroll_duration_and_wait_results_when_the_seek_is_skipped(self) -> None:
        timing = PlaybackTiming(
            1.0,
            1.1,
            "start",
            preroll_at=2.0,
            preroll_finished_at=7.0,
            preroll_wait="timeout",
            seek_wait="skipped",
            startup_finished_at=7.6,
        )
        with patch("builtins.print") as printer:
            timing.observe_output("output_observed", rendered_at=7.25)

        line = printer.call_args.args[0]
        self.assertIn("gst_preroll_wait=timeout", line)
        self.assertIn("gst_preroll_ms=5000", line)
        self.assertIn("gst_seek_wait=skipped", line)
        self.assertNotIn("gst_seek_ms=", line)

    def test_logs_preroll_phases_and_available_buffer_milestones(self) -> None:
        timing = PlaybackTiming(
            1.0,
            1.1,
            "start",
            preroll_at=2.0,
            pause_returned_at=2.2,
            preroll_finished_at=4.0,
            preroll_milestones=PrerollMilestones(
                source_setup_at=2.1,
                first_source_buffer_at=2.2,
                format_detected_at=2.3,
                demuxer_factories=("id3demux", "qtdemux"),
                first_demux_input_at=2.4,
                first_demux_input_factory="id3demux",
                first_demux_output_at=2.6,
                first_demux_output_factory="id3demux",
                first_demux_audio_at=2.9,
                first_demux_audio_factory="qtdemux",
                first_parser_input_at=3.0,
                first_parser_output_at=3.1,
                first_decoder_input_at=3.2,
                first_decoder_input_factory="avdec_aac",
                first_decoder_output_at=3.3,
                first_decoded_audio_at=3.4,
                first_sink_buffer_at=3.5,
            ),
            startup_finished_at=4.4,
        )
        with patch("builtins.print") as printer:
            timing.observe_output("output_observed", rendered_at=4.3)
        fields = printer.call_args.args[0].split()
        for metric in (
            "gst_preroll_ms=2000",
            "gst_pause_call_ms=200",
            "gst_preroll_wait_ms=1800",
            "gst_source_setup_ms=100",
            "gst_first_source_buffer_ms=200",
            "gst_format_detected_ms=300",
            "gst_demuxers=id3demux,qtdemux",
            "gst_first_demux_input_ms=400",
            "gst_demux_input_factory=id3demux",
            "gst_first_demux_output_ms=600",
            "gst_demux_output_factory=id3demux",
            "gst_first_demux_audio_ms=900",
            "gst_demux_audio_factory=qtdemux",
            "gst_first_parser_input_ms=1000",
            "gst_first_parser_output_ms=1100",
            "gst_first_decoder_input_ms=1200",
            "gst_decoder_factory=avdec_aac",
            "gst_first_decoder_output_ms=1300",
            "gst_first_decoded_audio_ms=1400",
            "gst_source_to_decoded_audio_ms=1200",
            "gst_first_sink_buffer_ms=1500",
            "gst_after_first_sink_buffer_ms=500",
        ):
            self.assertIn(metric, fields)
        self.assertFalse(any(field.startswith("gst_audio_sink_added_ms=") for field in fields))

    def test_source_read_trace_follows_its_playback_timing_line(self) -> None:
        timing = PlaybackTiming(
            1.0,
            1.1,
            "start",
            preroll_at=2.0,
            preroll_milestones=PrerollMilestones(
                source_reads=SourceReadTrace(
                    start_at=2.1,
                    end_at=2.5,
                    start_reason="first_input",
                    end_reason="first_audio",
                    buffers=(SourceBuffer(2.25, 100, 900, "push"),),
                    events=(),
                )
            ),
            startup_finished_at=3.0,
        )
        with patch("builtins.print") as printer:
            timing.observe_output("output_observed", rendered_at=2.9)
            timing.report()

        timing_line, summary, detail = [call.args[0] for call in printer.call_args_list]
        self.assertTrue(timing_line.startswith("Playback timing "))
        self.assertTrue(summary.startswith("Playback source reads start_ms=100 end_ms=500 "))
        self.assertIn("bytes_range=900-999", detail)

    def test_omits_unobserved_audio_stages_when_preroll_times_out(self) -> None:
        timing = PlaybackTiming(
            1.0,
            1.1,
            "continue",
            preroll_at=2.0,
            preroll_finished_at=7.0,
            preroll_wait="timeout",
            preroll_milestones=PrerollMilestones(
                first_source_buffer_at=2.2,
                first_decoder_input_at=2.9,
            ),
            startup_finished_at=8.0,
        )
        with patch("builtins.print") as printer:
            timing.observe_output("output_observed", rendered_at=7.8)

        line = printer.call_args.args[0]
        self.assertIn("gst_first_decoder_input_ms=900", line.split())
        for name in (
            "gst_format_detected_ms",
            "gst_first_demux_audio_ms",
            "gst_first_parser_output_ms",
            "gst_first_decoder_output_ms",
            "gst_source_to_decoded_audio_ms",
        ):
            self.assertNotIn(f"{name}=", line)

    def test_finds_real_audio_sink_behind_wrappers_and_ignores_video_sinks(self) -> None:
        wrapper = Mock()
        wrapper.get_factory.return_value.get_metadata.return_value = "Sink/Audio"
        wrapper.find_property.return_value = None
        video = Mock()
        video.get_factory.return_value.get_metadata.return_value = "Sink/Video"
        audio = Mock()
        audio.get_factory.return_value.get_metadata.return_value = "Sink/Audio"
        iterator = Mock()
        iterator.next.side_effect = [
            ("ok", wrapper),
            ("resync", None),
            ("ok", video),
            ("ok", audio),
        ]
        gst = SimpleNamespace(IteratorResult=SimpleNamespace(OK="ok", RESYNC="resync"))

        self.assertIs(find_audio_sink(gst, Mock(iterate_recurse=lambda: iterator)), audio)
        iterator.resync.assert_called_once_with()
