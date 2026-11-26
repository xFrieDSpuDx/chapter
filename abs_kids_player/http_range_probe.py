"""Observe safe HTTP range metadata posted by the GStreamer source."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


HTTP_RESPONSE_LIMIT = 24
REQUEST_RANGE = re.compile(r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", re.IGNORECASE)
CONTENT_RANGE = re.compile(r"bytes ((?:[0-9]+-[0-9]+|\*)/(?:[0-9]+|\*))", re.IGNORECASE)


@dataclass(frozen=True)
class HttpResponse:
    at: float
    fields: tuple[str, ...]


@dataclass(frozen=True)
class HttpRangeTrace:
    available: bool
    source_factory: str | None
    responses: tuple[HttpResponse, ...]
    truncated: bool = False

    def log_fields(self) -> list[str]:
        fields = [
            f"gst_http_capture={'enabled' if self.available else 'unavailable'}",
            f"gst_http_responses={len(self.responses)}",
            f"gst_http_truncated={int(self.truncated)}",
        ]
        if self.source_factory is not None:
            fields.append(f"gst_http_source={self.source_factory}")
        return fields

    def log_lines(
        self, preroll_at: float, seek_at: float | None, playing_at: float | None
    ) -> list[str]:
        lines = []
        for index, response in enumerate(self.responses, start=1):
            if playing_at is not None and response.at >= playing_at:
                phase = "playing"
            elif seek_at is not None and response.at >= seek_at:
                phase = "seek"
            else:
                phase = "preroll"
            offset_ms = (response.at - preroll_at) * 1000
            lines.append(
                f"Playback HTTP range index={index} at_ms={offset_ms:.0f} phase={phase}"
                f" {' '.join(response.fields)}"
            )
        return lines


class HttpRangeProbe:
    """Subscribe to element messages without consuming them from the bus."""

    def __init__(self, playbin: Any, clock: Callable[[], float]) -> None:
        self._playbin = playbin
        self._clock = clock
        self._lock = threading.Lock()
        self._subscription: tuple[Any, int] | None = None
        self._closed = False
        self._responses: list[HttpResponse] = []
        self._truncated = False

    def attach(self) -> None:
        try:
            bus = self._playbin.get_bus()
            handler = bus.connect("sync-message::element", self._observe_message)
        except Exception:
            return
        try:
            bus.enable_sync_message_emission()
        except Exception:
            _disconnect(bus, handler)
            return
        self._subscription = (bus, handler)

    def finish(self) -> HttpRangeTrace:
        with self._lock:
            self._closed = True
            responses = tuple(sorted(self._responses, key=lambda response: response.at))
            truncated = self._truncated
        subscription = self._subscription
        if subscription is not None:
            bus, handler = subscription
            self._subscription = None
            _disconnect(bus, handler)
            try:
                bus.disable_sync_message_emission()
            except Exception:
                pass
        return HttpRangeTrace(
            subscription is not None, _source_factory(self._playbin), responses, truncated
        )

    def _observe_message(self, _bus: Any, message: Any) -> None:
        at = self._clock()
        try:
            fields = http_range_fields(message.get_structure())
        except Exception:
            return
        if not fields:
            return
        with self._lock:
            if self._closed:
                return
            if len(self._responses) >= HTTP_RESPONSE_LIMIT:
                self._truncated = True
            else:
                self._responses.append(HttpResponse(at, fields))


def http_range_fields(structure: Any) -> tuple[str, ...]:
    if structure is None or structure.get_name() != "http-headers":
        return ()
    fields = []
    try:
        has_status, status = structure.get_uint("http-status-code")
        if has_status and 100 <= status <= 599:
            fields.append(f"status={status}")
    except Exception:
        pass
    request = _header(structure, "request-headers", "range")
    response = _header(structure, "response-headers", "content-range")
    if request and REQUEST_RANGE.fullmatch(request):
        fields.append(f"request_range={request}")
    if response and (match := CONTENT_RANGE.fullmatch(response)):
        fields.append(f"content_range={match[1]}")
    return tuple(fields)


def _header(structure: Any, field: str, name: str) -> str | None:
    try:
        headers = structure.get_value(field)
        if headers is not None:
            for index in range(headers.n_fields()):
                key = headers.nth_field_name(index)
                if key.lower() == name:
                    value = headers.get_string(key)
                    if isinstance(value, str) and len(value) <= 128:
                        return value
    except Exception:
        # A missing header or binding must not hide the other fields.
        pass
    return None


def _source_factory(playbin: Any) -> str | None:
    try:
        source = playbin.get_property("source")
        name = source.get_factory().get_name()
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            return name
    except Exception:
        pass
    return None


def _disconnect(bus: Any, handler: int) -> None:
    try:
        bus.disconnect(handler)
    except Exception:
        pass
