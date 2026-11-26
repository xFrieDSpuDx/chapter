from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from abs_kids_player.preroll_probe import PrerollMilestones, PrerollProbe


class Pad:
    def __init__(self, direction="src", media_type=None, *, name="src", stream_type=None) -> None:
        self.probes = {}
        self.added_count = 0
        self.on_add = None
        self.direction = direction
        self.media_type = media_type
        self.name = name
        self.stream_type = stream_type

    def get_name(self):
        return self.name

    def get_stream(self):
        if self.stream_type is None:
            return None
        return SimpleNamespace(get_stream_type=lambda: self.stream_type)

    def get_direction(self):
        return self.direction

    def get_current_caps(self):
        if self.media_type is None:
            return None
        return SimpleNamespace(
            get_size=lambda: 1,
            get_structure=lambda _index: SimpleNamespace(get_name=lambda: self.media_type),
        )

    def add_probe(self, flags, callback):
        self.added_count += 1
        probe_id = self.added_count
        self.probes[probe_id] = (flags, callback)
        if self.on_add is not None:
            self.on_add()
        return probe_id

    def remove_probe(self, probe_id):
        del self.probes[probe_id]

    def send_buffer(self, probe_info=None):
        return [callback(self, probe_info) for _, callback in self.probes.values()]


class SignalEmitter:
    def __init__(self) -> None:
        self.handlers = {}

    def connect(self, signal, callback):
        handler = len(self.handlers) + 1
        self.handlers[handler] = (signal, callback)
        return handler

    def disconnect(self, handler):
        del self.handlers[handler]

    def emit(self, signal, *args):
        for registered_signal, callback in list(self.handlers.values()):
            if registered_signal == signal:
                callback(self, *args)


class Element(SignalEmitter):
    def __init__(self, pads, *, name="", classes="", has_stats=False) -> None:
        super().__init__()
        self.pads = pads
        self.factory = SimpleNamespace(get_name=lambda: name, get_metadata=lambda _key: classes)
        self.has_stats = has_stats

    def get_static_pad(self, name):
        return self.pads.get(name)

    def get_factory(self):
        return self.factory

    def find_property(self, _name):
        return object() if self.has_stats else None

    def foreach_src_pad(self, callback, user_data):
        for pad in self.pads.values():
            if pad.direction == "src" and not callback(self, pad, user_data):
                break


class Playbin(SignalEmitter):
    def __init__(self, **properties) -> None:
        super().__init__()
        self.properties = properties

    def get_property(self, name):
        return self.properties.get(name)


class PrerollProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gst = SimpleNamespace(
            PadProbeType=SimpleNamespace(BUFFER=16, BUFFER_LIST=32, EVENT_BOTH=192, PULL=8192),
            PadProbeReturn=SimpleNamespace(OK="ok"),
            PadDirection=SimpleNamespace(SRC="src"),
            StreamType=SimpleNamespace(AUDIO=2),
            EventType=SimpleNamespace(),
        )
        self.clock = Mock(return_value=10.0)

    def test_records_first_buffers_and_cleans_up_all_subscriptions(self) -> None:
        source, decoded, wrong_sink, audio_sink = Pad(), Pad(), Pad(), Pad()
        playbin = Playbin(**{"audio-filter": Element({"sink": decoded})})
        probe = PrerollProbe(self.gst, playbin, self.clock)
        probe.attach()

        self.clock.return_value = 10.1
        playbin.emit("source-setup", Element({"src": source}))
        self.clock.return_value = 10.4
        self.assertEqual(source.send_buffer(), ["ok"])
        self.clock.return_value = 10.6
        source.send_buffer()
        self.assertEqual(decoded.send_buffer(), ["ok"])
        playbin.emit(
            "element-setup", Element({"sink": wrong_sink}, classes="Sink/Video", has_stats=True)
        )
        self.assertEqual(wrong_sink.added_count, 0)
        self.clock.return_value = 10.7
        playbin.emit(
            "element-setup", Element({"sink": audio_sink}, classes="Sink/Audio", has_stats=True)
        )
        self.clock.return_value = 10.8
        self.assertEqual(audio_sink.send_buffer(), ["ok"])

        self.assertEqual(source.probes[1][0], 16 | 32)
        self.assertEqual(
            probe.finish(11.0),
            PrerollMilestones(
                source_setup_at=10.1,
                first_source_buffer_at=10.4,
                first_decoded_audio_at=10.6,
                audio_sink_added_at=10.7,
                first_sink_buffer_at=10.8,
            ),
        )
        self.assertEqual(playbin.handlers, {})
        for pad in (source, decoded, audio_sink):
            self.assertEqual(pad.probes, {})

    def test_existing_audio_sink_is_probed_once(self) -> None:
        pad = Pad()
        sink = Element({"sink": pad}, classes="Sink/Audio", has_stats=True)
        playbin = Playbin(**{"audio-sink": sink})
        probe = PrerollProbe(self.gst, playbin, self.clock)
        probe.attach()
        playbin.emit("element-setup", sink)
        self.assertEqual(pad.added_count, 1)
        pad.send_buffer()
        self.assertEqual(probe.finish(10.1).first_sink_buffer_at, 10.0)
        self.assertEqual(pad.probes, {})

    def test_late_buffers_are_excluded_from_a_timed_out_preroll(self) -> None:
        source = Pad()
        playbin = Playbin()
        probe = PrerollProbe(self.gst, playbin, self.clock)
        probe.attach()
        playbin.emit("source-setup", Element({"src": source}))
        _, callback = source.probes[1]
        self.clock.return_value = 15.1
        source.send_buffer()

        first = probe.finish(15.0)
        self.assertEqual(first, PrerollMilestones(source_setup_at=10.0))
        self.assertEqual(source.probes, {})
        self.assertEqual(callback(source, None), "ok")
        self.assertEqual(probe.finish(15.2), first)

    def test_cleanup_removes_a_probe_registered_while_finishing(self) -> None:
        source = Pad()
        playbin = Playbin()
        probe = PrerollProbe(self.gst, playbin, self.clock)
        probe.attach()
        source.on_add = lambda: probe.finish(10.0)
        playbin.emit("source-setup", Element({"src": source}))

        self.assertEqual(source.probes, {})
        self.assertEqual(playbin.handlers, {})

    def test_records_format_detection_and_first_audio_buffers_at_each_stage(self) -> None:
        typefinder = Element({}, name="typefind")
        demux_input = Pad(direction="sink")
        demuxer = Element({"sink": demux_input}, name="qtdemux", classes="Codec/Demuxer")
        demux_audio, demux_video = Pad(), Pad(media_type="video/x-h264")
        parser_input, parser_output = Pad(direction="sink"), Pad()
        parser = Element({"sink": parser_input, "src": parser_output}, classes="Codec/Parser/Audio")
        decoder_input, decoder_output = Pad(direction="sink"), Pad()
        decoder = Element(
            {"sink": decoder_input, "src": decoder_output}, name="avdec_aac", classes="Codec/Decoder/Audio"
        )
        playbin = Playbin()
        probe = PrerollProbe(self.gst, playbin, self.clock)
        probe.attach()
        for pipeline_element in (typefinder, demuxer, parser, decoder):
            playbin.emit("element-setup", pipeline_element)

        self.clock.return_value = 10.25
        typefinder.emit("have-type", 100, object())
        demuxer.emit("pad-added", demux_audio)
        demuxer.emit("pad-added", demux_video)
        demuxer.emit("pad-added", demux_input)
        self.assertEqual(demux_input.added_count, 1)
        demux_audio.send_buffer()  # The pad has not announced its caps yet.
        demux_video.send_buffer()
        demux_audio.media_type = "audio/mpeg"
        for when, pad in (
            (10.5, demux_input),
            (12.0, demux_audio),
            (12.1, parser_input),
            (12.4, parser_output),
            (12.5, decoder_input),
            (12.8, decoder_output),
        ):
            self.clock.return_value = when
            results = pad.send_buffer()
            self.assertEqual(results, ["ok", "ok"] if pad is demux_audio else ["ok"])
        decoder_output.send_buffer()
        typefinder.emit("have-type", 100, object())

        self.assertEqual(
            probe.finish(13.0),
            PrerollMilestones(
                format_detected_at=10.25,
                demuxer_factories=("qtdemux",),
                first_demux_input_at=10.5,
                first_demux_input_factory="qtdemux",
                first_demux_output_at=10.25,
                first_demux_output_factory="qtdemux",
                first_demux_audio_at=12.0,
                first_demux_audio_factory="qtdemux",
                first_parser_input_at=12.1,
                first_parser_output_at=12.4,
                first_decoder_input_at=12.5,
                first_decoder_input_factory="avdec_aac",
                first_decoder_output_at=12.8,
            ),
        )
        for watched_element in (playbin, typefinder, demuxer):
            self.assertEqual(watched_element.handlers, {})
        for pad in (
            demux_input,
            demux_audio,
            demux_video,
            parser_input,
            parser_output,
            decoder_input,
            decoder_output,
        ):
            self.assertEqual(pad.probes, {})

    def test_observes_an_existing_demux_pad_once_and_ignores_video_decoders(self) -> None:
        audio = Pad(media_type="audio/mpeg")
        video = Pad()
        demuxer = Element({"existing_audio": audio}, name="oggdemux", classes="Codec/Demuxer")
        decoder = Element({"src": video}, classes="Codec/Decoder/Video")
        playbin = Playbin()
        probe = PrerollProbe(self.gst, playbin, self.clock)
        probe.attach()

        playbin.emit("element-setup", demuxer)
        playbin.emit("element-setup", decoder)
        demuxer.emit("pad-added", audio)
        self.assertEqual(audio.added_count, 2)
        self.assertEqual(video.added_count, 0)

        audio.send_buffer()
        self.assertEqual(
            probe.finish(10.1),
            PrerollMilestones(
                demuxer_factories=("oggdemux",),
                first_demux_output_at=10.0,
                first_demux_output_factory="oggdemux",
                first_demux_audio_at=10.0,
                first_demux_audio_factory="oggdemux",
            ),
        )

    def test_pairs_audio_output_with_its_demuxer_without_audio_caps(self) -> None:
        for audio in (
            Pad(name="audio_0"),
            Pad(media_type="application/x-cenc", stream_type=self.gst.StreamType.AUDIO),
        ):
            with self.subTest(pad=audio.name):
                self.clock.return_value = 10.0
                untyped = Pad()
                tag_demux = Element({"src": untyped}, name="id3demux", classes="Codec/Demuxer")
                audio_demux = Element({}, name="qtdemux", classes="Codec/Demuxer")
                playbin = Playbin()
                probe = PrerollProbe(self.gst, playbin, self.clock)
                probe.attach()
                playbin.emit("element-setup", tag_demux)
                playbin.emit("element-setup", audio_demux)
                audio_demux.emit("pad-added", audio)

                self.clock.return_value = 10.2
                untyped.send_buffer()
                self.clock.return_value = 10.4
                audio.send_buffer()

                self.assertEqual(
                    probe.finish(11.0),
                    PrerollMilestones(
                        demuxer_factories=("id3demux", "qtdemux"),
                        first_demux_output_at=10.2,
                        first_demux_output_factory="id3demux",
                        first_demux_audio_at=10.4,
                        first_demux_audio_factory="qtdemux",
                    ),
                )

    def test_retains_demuxer_name_on_timeout_and_excludes_late_output(self) -> None:
        audio = Pad(name="audio_0")
        demuxer = Element({"src": audio}, name="qtdemux", classes="Codec/Demuxer")
        late_demuxer = Element({}, name="matroskademux", classes="Codec/Demuxer")
        playbin = Playbin()
        probe = PrerollProbe(self.gst, playbin, self.clock)
        probe.attach()
        playbin.emit("element-setup", demuxer)
        self.clock.return_value = 15.1
        audio.send_buffer()
        playbin.emit("element-setup", late_demuxer)

        self.assertEqual(probe.finish(15.0), PrerollMilestones(demuxer_factories=("qtdemux",)))

    def test_source_trace_uses_qtdemux_window_even_with_an_earlier_demuxer(self) -> None:
        source, metadata_input, demux_input, audio = (
            Pad(),
            Pad("sink"),
            Pad("sink"),
            Pad(name="audio_0"),
        )
        playbin = Playbin()
        probe = PrerollProbe(self.gst, playbin, self.clock, trace_source_reads=True)
        probe.attach()
        playbin.emit("source-setup", Element({"src": source}, name="souphttpsrc"))
        playbin.emit(
            "element-setup",
            Element({"sink": metadata_input}, name="id3demux", classes="Codec/Demuxer/Metadata"),
        )
        metadata_input.send_buffer()
        self.clock.return_value = 10.1
        playbin.emit(
            "element-setup",
            Element({"sink": demux_input, "src": audio}, name="qtdemux", classes="Codec/Demuxer"),
        )
        raw = SimpleNamespace(offset=200, get_size=lambda: 200)
        info = SimpleNamespace(type=16, get_buffer=lambda: raw)
        source.send_buffer(info)
        self.clock.return_value = 10.2
        demux_input.send_buffer()
        self.clock.return_value = 10.5
        source.send_buffer(info)
        self.clock.return_value = 10.7
        audio.send_buffer()
        self.clock.return_value = 11.0
        source.send_buffer(info)

        preroll = probe.finish(11.1)
        self.assertEqual(preroll.first_demux_input_factory, "id3demux")
        trace = preroll.source_reads
        self.assertEqual(trace.start_at, 10.2)
        self.assertEqual(trace.end_at, 10.7)
        self.assertEqual(trace.end_reason, "first_audio")
        self.assertEqual([sample.at for sample in trace.buffers], [10.5])
        self.assertEqual(source.probes, {})

    def test_source_trace_reports_silence_until_preroll_timeout(self) -> None:
        playbin = Playbin()
        probe = PrerollProbe(self.gst, playbin, self.clock, trace_source_reads=True)
        probe.attach()
        playbin.emit("source-setup", Element({"src": Pad()}, name="souphttpsrc"))
        playbin.emit("element-setup", Element({}, name="qtdemux", classes="Codec/Demuxer"))

        trace = probe.finish(15.0).source_reads
        self.assertEqual(trace.start_reason, "element_added")
        self.assertEqual(trace.end_reason, "preroll_end")
        self.assertIn("buffers=0 bytes=0", trace.log_lines(10.0)[0])
        self.assertIn("max_gap_ms=5000 tail_gap_ms=5000", trace.log_lines(10.0)[0])

    def test_source_trace_requires_a_source_probe_and_qtdemux(self) -> None:
        for pads, factory in (({}, "qtdemux"), ({"src": Pad()}, "oggdemux")):
            with self.subTest(factory=factory):
                playbin = Playbin()
                probe = PrerollProbe(self.gst, playbin, self.clock, trace_source_reads=True)
                probe.attach()
                playbin.emit("source-setup", Element(pads, name="souphttpsrc"))
                playbin.emit("element-setup", Element({}, name=factory, classes="Codec/Demuxer"))
                self.assertIsNone(probe.finish(15.0).source_reads)

    def test_cleanup_disconnects_an_element_signal_registered_while_finishing(self) -> None:
        typefinder = Element({}, name="typefind")
        playbin = Playbin()
        probe = PrerollProbe(self.gst, playbin, self.clock)
        probe.attach()
        connect = typefinder.connect

        def connect_and_finish(signal, callback):
            handler = connect(signal, callback)
            probe.finish(10.0)
            return handler

        typefinder.connect = connect_and_finish
        playbin.emit("element-setup", typefinder)

        self.assertEqual(typefinder.handlers, {})
        self.assertEqual(playbin.handlers, {})

    def test_missing_gstreamer_hooks_leave_timing_empty(self) -> None:
        probe = PrerollProbe(self.gst, object(), self.clock)
        probe.attach()
        self.assertEqual(probe.finish(10.0), PrerollMilestones())
