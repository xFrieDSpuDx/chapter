"""Capture source pad activity without reading media or changing data flow."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .http_range_probe import http_range_fields


BUFFER_SAMPLE_LIMIT = 2048
RANGE_EVENT_LIMIT = 16
DETAIL_GAP_COUNT = 4
DETAIL_OFFSET_JUMP_COUNT = 3
UNKNOWN_OFFSET = (1 << 64) - 1


@dataclass(frozen=True)
class SourceBuffer:
    at: float
    size: int
    offset: int | None
    mode: str


@dataclass(frozen=True)
class RangeEvent:
    at: float
    kind: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class SourceReadTrace:
    start_at: float
    end_at: float
    start_reason: str
    end_reason: str
    buffers: tuple[SourceBuffer, ...]
    events: tuple[RangeEvent, ...]
    buffers_truncated: bool = False
    events_truncated: bool = False

    def log_lines(self, preroll_at: float) -> list[str]:
        times = [self.start_at, *(buffer.at for buffer in self.buffers)]
        gaps = [end - start for start, end in zip(times, times[1:])]
        tail_gap = self.end_at - times[-1]
        summary = (
            "Playback source reads"
            f" start_ms={_milliseconds(self.start_at - preroll_at)}"
            f" end_ms={_milliseconds(self.end_at - preroll_at)}"
            f" start_reason={self.start_reason} end_reason={self.end_reason}"
            f" buffers={len(self.buffers)}"
            f" bytes={sum(buffer.size for buffer in self.buffers)}"
            f" buffers_truncated={int(self.buffers_truncated)}"
            f" events_truncated={int(self.events_truncated)}"
        )
        if not self.buffers_truncated:
            summary += (
                f" max_gap_ms={_milliseconds(max([tail_gap, *gaps]))}"
                f" tail_gap_ms={_milliseconds(tail_gap)}"
            )
        details = [
            (event.at, f"kind={event.kind} {' '.join(event.fields)}")
            for event in self.events
            if event.at >= preroll_at
        ]
        if self.buffers:
            longest_gaps = sorted(range(len(gaps)), key=gaps.__getitem__, reverse=True)
            offset_jumps = _offset_jumps(self.buffers)
            samples = {
                0,
                len(gaps) - 1,
                *longest_gaps[:DETAIL_GAP_COUNT],
                *sorted(offset_jumps)[:DETAIL_OFFSET_JUMP_COUNT],
            }
            for index in sorted(samples):
                buffer = self.buffers[index]
                fields = (
                    f"kind=buffer index={index + 1} mode={buffer.mode} bytes={buffer.size}"
                    f" gap_ms={_milliseconds(gaps[index])}"
                )
                if buffer.offset is not None:
                    fields += f" bytes_range={buffer.offset}-{buffer.offset + buffer.size - 1}"
                if index in offset_jumps:
                    fields += " offset_jump=1"
                details.append((buffer.at, fields))
        return [
            summary,
            *(
                f"Playback source read at_ms={_milliseconds(at - preroll_at)} {fields}"
                for at, fields in sorted(details, key=lambda detail: detail[0])
            ),
        ]


class SourceReadProbe:
    """Collect bounded observations from the source until preroll ends."""

    def __init__(self, gst: Any, clock: Callable[[], float]) -> None:
        self._gst = gst
        self._clock = clock
        self._lock = threading.Lock()
        self._buffers: list[SourceBuffer] = []
        self._events: list[RangeEvent] = []
        self._first_buffer_dropped_at: float | None = None
        self._first_event_dropped_at: float | None = None

    def callback(self, factory: str) -> Callable[..., Any]:
        # These sources define buffer offsets in bytes even before a BYTES segment.
        byte_offset_source = factory in {"souphttpsrc", "filesrc"}
        byte_offsets = byte_offset_source
        flags = self._gst.PadProbeType
        event_types = self._gst.EventType

        def observe(_pad: Any, info: Any, *_user_data: Any) -> Any:
            nonlocal byte_offsets
            at = self._clock()
            try:
                if info.type & (flags.BUFFER | flags.BUFFER_LIST):
                    self._observe_buffers(at, info, byte_offsets)
                else:
                    event = info.get_event()
                    if event.type == event_types.SEGMENT:
                        byte_offsets = (
                            byte_offset_source
                            or event.parse_segment().format == self._gst.Format.BYTES
                        )
                    elif event.type == event_types.SEEK:
                        self._observe_seek(at, event)
                    elif event.type == event_types.CUSTOM_DOWNSTREAM_STICKY:
                        self._observe_http(at, event.get_structure())
            except Exception:
                # Some plugins and bindings omit optional timing metadata.
                pass
            return self._gst.PadProbeReturn.OK

        return observe

    def snapshot(
        self, start: float, end: float, *, start_reason: str, end_reason: str
    ) -> SourceReadTrace:
        with self._lock:
            buffers = tuple(
                sorted(
                    (sample for sample in self._buffers if start <= sample.at <= end),
                    key=lambda sample: sample.at,
                )
            )
            events = tuple(event for event in self._events if event.at <= end)
            return SourceReadTrace(
                start,
                end,
                start_reason,
                end_reason,
                buffers,
                events,
                buffers_truncated=_dropped_before(self._first_buffer_dropped_at, end),
                events_truncated=_dropped_before(self._first_event_dropped_at, end),
            )

    def _observe_buffers(self, at: float, info: Any, byte_offsets: bool) -> None:
        flags = self._gst.PadProbeType
        pull = bool(info.type & flags.PULL)
        if info.type & flags.BUFFER_LIST:
            batch = info.get_buffer_list()
            buffers = (batch.get(index) for index in range(batch.length()))
        else:
            buffers = (info.get_buffer(),)
        # A pulled read supplies a byte offset in probe info even if the buffer omits it.
        pulled_offset = (
            _byte_offset(getattr(info, "offset", None))
            if pull and info.type & flags.BUFFER
            else None
        )
        with self._lock:
            for buffer in buffers:
                if len(self._buffers) >= BUFFER_SAMPLE_LIMIT:
                    if self._first_buffer_dropped_at is None or at < self._first_buffer_dropped_at:
                        self._first_buffer_dropped_at = at
                    break
                size = buffer.get_size()
                if size <= 0:
                    continue
                offset = pulled_offset
                if offset is None and byte_offsets:
                    offset = _byte_offset(getattr(buffer, "offset", None))
                self._buffers.append(SourceBuffer(at, size, offset, "pull" if pull else "push"))

    def _observe_seek(self, at: float, event: Any) -> None:
        _rate, fmt, _flags, start_type, start, stop_type, stop = event.parse_seek()
        if fmt != self._gst.Format.BYTES or start_type != self._gst.SeekType.SET:
            return
        offset = _byte_offset(start)
        if offset is None:
            return
        fields = [f"byte_start={offset}"]
        if stop_type == self._gst.SeekType.SET and _byte_offset(stop) is not None:
            fields.append(f"byte_stop_exclusive={stop}")
        self._record_event(RangeEvent(at, "byte_seek", tuple(fields)))

    def _observe_http(self, at: float, structure: Any) -> None:
        fields = http_range_fields(structure)
        if fields:
            self._record_event(RangeEvent(at, "http_headers", fields))

    def _record_event(self, event: RangeEvent) -> None:
        with self._lock:
            if len(self._events) < RANGE_EVENT_LIMIT:
                self._events.append(event)
            elif self._first_event_dropped_at is None or event.at < self._first_event_dropped_at:
                self._first_event_dropped_at = event.at


def _offset_jumps(buffers: tuple[SourceBuffer, ...]) -> set[int]:
    return {
        index
        for index, (previous, current) in enumerate(zip(buffers, buffers[1:]), start=1)
        if previous.offset is not None
        and current.offset is not None
        and previous.offset + previous.size != current.offset
    }


def _dropped_before(dropped_at: float | None, cutoff: float) -> bool:
    return dropped_at is not None and dropped_at <= cutoff


def _byte_offset(value: int | None) -> int | None:
    return value if value is not None and 0 <= value < UNKNOWN_OFFSET else None


def _milliseconds(seconds: float) -> str:
    return f"{seconds * 1000:.0f}"
