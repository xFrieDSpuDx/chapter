from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from abs_kids_player.http_range_probe import HttpRangeProbe
from abs_kids_player.source_read_probe import UNKNOWN_OFFSET, SourceBuffer, SourceReadProbe


class Structure:
    def __init__(self, name: str = "", **fields: object) -> None:
        self.name = name
        self.fields = fields

    def get_name(self) -> str:
        return self.name

    def get_uint(self, key: str) -> tuple[bool, object]:
        return key in self.fields, self.fields.get(key)

    def get_value(self, key: str) -> object:
        return self.fields.get(key)

    def get_string(self, key: str) -> object:
        return self.fields.get(key)

    def n_fields(self) -> int:
        return len(self.fields)

    def nth_field_name(self, index: int) -> str:
        return tuple(self.fields)[index]


class SourceReadProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gst = SimpleNamespace(
            PadProbeType=SimpleNamespace(BUFFER=16, BUFFER_LIST=32, PULL=8192),
            PadProbeReturn=SimpleNamespace(OK="ok"),
            EventType=SimpleNamespace(
                SEGMENT="segment", SEEK="seek", CUSTOM_DOWNSTREAM_STICKY="sticky"
            ),
            Format=SimpleNamespace(BYTES="bytes"),
            SeekType=SimpleNamespace(SET="set"),
        )
        self.clock = Mock(return_value=10.0)
        self.probe = SourceReadProbe(self.gst, self.clock)

    def snapshot(self, start: float, end: float):
        return self.probe.snapshot(start, end, start_reason="first_input", end_reason="first_audio")

    def test_logs_gap_sizes_byte_seeks_and_only_safe_http_range_fields(self) -> None:
        observe = self.probe.callback("souphttpsrc")
        headers = Structure(
            "http-headers",
            **{
                "uri": "https://books.test/book?token=secret",
                "http-status-code": 206,
                "request-headers": Structure(**{"rAnGe": "bytes=600-", "Authorization": "secret"}),
                "response-headers": Structure(**{"Content-Range": "bytes 600-899/900"}),
            },
        )
        seek = SimpleNamespace(
            type="seek", parse_seek=lambda: (1.0, "bytes", 0, "set", 600, "set", 900)
        )
        self.clock.return_value = 10.1
        observe(None, buffer_info(0, 100))
        self.clock.return_value = 10.4
        observe(None, buffer_info(100, 100))
        self.clock.return_value = 10.5
        observe(None, event_info(seek))
        self.clock.return_value = 10.9
        observe(None, event_info(SimpleNamespace(type="sticky", get_structure=lambda: headers)))
        self.clock.return_value = 11.1
        observe(None, buffer_info(600, 300))
        self.clock.return_value = 12.0
        observe(None, buffer_info(900, 200))

        lines = self.snapshot(10.2, 11.5).log_lines(10.0)
        self.assertIn("buffers=2 bytes=400", lines[0])
        self.assertIn("max_gap_ms=700 tail_gap_ms=400", lines[0])
        self.assertIn("kind=byte_seek byte_start=600 byte_stop_exclusive=900", lines[2])
        self.assertIn(
            "kind=http_headers status=206 request_range=bytes=600- content_range=600-899/900",
            lines[3],
        )
        self.assertIn("gap_ms=700 bytes_range=600-899 offset_jump=1", lines[4])
        self.assertNotIn("secret", "\n".join(lines))
        self.assertNotIn("books.test", "\n".join(lines))

    def test_pulled_offsets_and_byte_segments_supply_ranges_when_known(self) -> None:
        observe = self.probe.callback("customsrc")
        observe(None, buffer_info(42, 9))  # Offsets have an unknown unit.
        segment = SimpleNamespace(
            type="segment", parse_segment=lambda: SimpleNamespace(format="bytes")
        )
        self.clock.return_value = 10.1
        observe(None, event_info(segment))
        observe(None, buffer_info(42, 9))
        observe(None, buffer_info(UNKNOWN_OFFSET, 7))
        self.clock.return_value = 10.2
        observe(
            None,
            SimpleNamespace(
                type=8192 | 16, offset=400, get_buffer=lambda: buffer(UNKNOWN_OFFSET, 3)
            ),
        )
        batch = SimpleNamespace(
            length=lambda: 2, get=lambda index: buffer(600 if index == 0 else UNKNOWN_OFFSET, 4)
        )
        self.clock.return_value = 10.3
        observe(None, SimpleNamespace(type=32, get_buffer_list=lambda: batch))

        self.assertEqual(
            self.snapshot(10.0, 11.0).buffers,
            (
                SourceBuffer(10.0, 9, None, "push"),
                SourceBuffer(10.1, 9, 42, "push"),
                SourceBuffer(10.1, 7, None, "push"),
                SourceBuffer(10.2, 3, 400, "pull"),
                SourceBuffer(10.3, 4, 600, "push"),
                SourceBuffer(10.3, 4, None, "push"),
            ),
        )

    def test_ignores_time_seeks_and_malformed_range_values(self) -> None:
        observe = self.probe.callback("souphttpsrc")
        seek = SimpleNamespace(
            type="seek", parse_seek=lambda: (1.0, "time", 0, "set", 600, "set", 900)
        )
        headers = Structure(
            "http-headers",
            **{
                "request-headers": Structure(Range="bytes=1-9 token=hidden"),
                "response-headers": Structure(**{"Content-Range": "bytes 1-9/40 hidden"}),
            },
        )
        self.assertEqual(observe(None, event_info(seek)), "ok")
        self.assertEqual(
            observe(
                None, event_info(SimpleNamespace(type="sticky", get_structure=lambda: headers))
            ),
            "ok",
        )
        self.assertEqual(self.snapshot(10.0, 11.0).events, ())

    def test_limits_capture_and_omits_unmeasured_tail_gaps(self) -> None:
        observe = self.probe.callback("souphttpsrc")
        with patch("abs_kids_player.source_read_probe.BUFFER_SAMPLE_LIMIT", 2):
            for index in range(4):
                self.clock.return_value = 10.0 + index / 10
                observe(None, buffer_info(index * 100, 100))
        self.assertFalse(self.snapshot(10.0, 10.15).buffers_truncated)

        trace = self.snapshot(10.0, 11.0)
        self.assertEqual(len(trace.buffers), 2)
        self.assertTrue(trace.buffers_truncated)
        self.assertIn("buffers_truncated=1", trace.log_lines(10.0)[0])
        self.assertNotIn("tail_gap_ms=", trace.log_lines(10.0)[0])


class HttpRangeProbeTest(unittest.TestCase):
    def test_subscribes_without_draining_messages_and_logs_only_range_metadata(self) -> None:
        source = SimpleNamespace(
            get_factory=lambda: SimpleNamespace(get_name=lambda: "souphttpsrc")
        )
        bus = Mock()
        bus.connect.return_value = 12
        playbin = SimpleNamespace(get_bus=lambda: bus, get_property=lambda _name: source)
        probe = HttpRangeProbe(playbin, lambda: 10.5)
        probe.attach()
        signal, observe = bus.connect.call_args.args
        self.assertEqual(signal, "sync-message::element")
        bus.enable_sync_message_emission.assert_called_once_with()

        headers = Structure(
            "http-headers",
            **{
                "uri": "https://books.test/book?token=private",
                "http-status-code": 206,
                "request-headers": Structure(Range="bytes=100-", Authorization="secret"),
                "response-headers": Structure(**{"content-range": "bytes 100-499/500"}),
            },
        )
        observe(bus, SimpleNamespace(get_structure=lambda: Structure("unrelated-message")))
        observe(bus, SimpleNamespace(get_structure=lambda: headers))
        trace = probe.finish()
        bus.disconnect.assert_called_once_with(12)
        bus.disable_sync_message_emission.assert_called_once_with()
        bus.pop.assert_not_called()

        self.assertIn("gst_http_source=souphttpsrc", trace.log_fields())
        self.assertIn("gst_http_responses=1", trace.log_fields())
        self.assertEqual(
            trace.log_lines(10.0, 10.4, 12.0),
            [
                "Playback HTTP range index=1 at_ms=500 phase=seek status=206 "
                "request_range=bytes=100- content_range=100-499/500"
            ],
        )

    def test_keeps_status_when_one_nested_header_is_unavailable_and_caps_trace(self) -> None:
        bus = Mock()
        bus.connect.return_value = 1
        playbin = SimpleNamespace(get_bus=lambda: bus, get_property=lambda _name: None)
        probe = HttpRangeProbe(playbin, lambda: 10.5)
        probe.attach()
        observe = bus.connect.call_args.args[1]
        headers = Structure(
            "http-headers",
            **{
                "http-status-code": 200,
                "request-headers": object(),
                "response-headers": Structure(**{"Content-Range": "invalid private value"}),
            },
        )
        message = SimpleNamespace(get_structure=lambda: headers)
        with patch("abs_kids_player.http_range_probe.HTTP_RESPONSE_LIMIT", 1):
            observe(bus, message)
            observe(bus, message)
        trace = probe.finish()
        observe(bus, message)

        self.assertTrue(trace.truncated)
        self.assertEqual([response.fields for response in trace.responses], [("status=200",)])
        self.assertEqual(len(probe.finish().responses), 1)

    def test_disconnects_if_the_native_bus_cannot_enable_sync_signals(self) -> None:
        bus = Mock()
        bus.connect.return_value = 7
        bus.enable_sync_message_emission.side_effect = RuntimeError("unsupported")
        probe = HttpRangeProbe(SimpleNamespace(get_bus=lambda: bus), lambda: 10.0)
        probe.attach()

        self.assertFalse(probe.finish().available)
        bus.disconnect.assert_called_once_with(7)
        bus.disable_sync_message_emission.assert_not_called()


def buffer(offset: int, size: int) -> SimpleNamespace:
    return SimpleNamespace(offset=offset, get_size=lambda: size)


def buffer_info(offset: int, size: int) -> SimpleNamespace:
    return SimpleNamespace(type=16, get_buffer=lambda: buffer(offset, size))


def event_info(event: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(type=64, get_event=lambda: event)
