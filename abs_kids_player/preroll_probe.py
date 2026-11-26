"""Observe media and audio buffers while playbin moves to PAUSED."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .source_read_probe import SourceReadProbe, SourceReadTrace


@dataclass(frozen=True)
class PrerollMilestones:
    source_setup_at: float | None = None
    first_source_buffer_at: float | None = None
    format_detected_at: float | None = None
    demuxer_factories: tuple[str, ...] = ()
    first_demux_input_at: float | None = None
    first_demux_input_factory: str | None = None
    first_demux_output_at: float | None = None
    first_demux_output_factory: str | None = None
    first_demux_audio_at: float | None = None
    first_demux_audio_factory: str | None = None
    first_parser_input_at: float | None = None
    first_parser_output_at: float | None = None
    first_decoder_input_at: float | None = None
    first_decoder_input_factory: str | None = None
    first_decoder_output_at: float | None = None
    first_decoded_audio_at: float | None = None
    audio_sink_added_at: float | None = None
    first_sink_buffer_at: float | None = None
    source_reads: SourceReadTrace | None = None


def is_rendering_audio_sink(element: Any) -> bool:
    factory = element.get_factory()
    if factory is None:
        return False
    classes = (factory.get_metadata("klass") or "").split("/")
    return "Audio" in classes and "Sink" in classes and element.find_property("stats") is not None


class PrerollProbe:
    """Attach temporary data probes without blocking or changing the pipeline."""

    def __init__(
        self,
        gst: Any,
        playbin: Any,
        clock: Callable[[], float],
        *,
        trace_source_reads: bool = False,
    ) -> None:
        self._gst = gst
        self._playbin = playbin
        self._clock = clock
        self._lock = threading.Lock()
        self._result: PrerollMilestones | None = None
        self._milestones: dict[str, float] = {}
        self._demuxers: dict[str, float] = {}
        self._milestone_factories: dict[str, str] = {}
        self._handlers: list[tuple[Any, int]] = []
        self._watched_pads: set[tuple[Any, str]] = set()
        self._pad_probes: list[tuple[Any, int]] = []
        self._source_reads = SourceReadProbe(gst, clock) if trace_source_reads else None
        self._source_trace_pad_attached = False
        self._qtdemux_at: dict[str, float] = {}

    def attach(self) -> None:
        for signal, callback in (
            ("source-setup", self._source_setup),
            ("element-setup", self._element_setup),
        ):
            self._watch_signal(self._playbin, signal, callback)
        for name, milestone in (
            ("audio-filter", "first_decoded_audio_at"),
            ("audio-sink", "first_sink_buffer_at"),
        ):
            try:
                element = self._playbin.get_property(name)
                if element is None or (
                    name == "audio-sink" and not is_rendering_audio_sink(element)
                ):
                    continue
            except Exception:
                continue
            self._watch_static_pad(element, "sink", milestone)

    def finish(self, ended_at: float) -> PrerollMilestones:
        # GStreamer can still be prerolling after get_state returns ASYNC.
        with self._lock:
            if self._result is not None:
                return self._result
            timestamps = {name: when for name, when in self._milestones.items() if when <= ended_at}
            factories = {
                name.removesuffix("_at") + "_factory": factory
                for name, factory in self._milestone_factories.items()
                if name in timestamps
            }
            milestones = PrerollMilestones(
                demuxer_factories=tuple(
                    name for name, when in self._demuxers.items() if when <= ended_at
                ),
                source_reads=self._source_read_snapshot(ended_at),
                **timestamps,
                **factories,
            )
            self._result = milestones
            handlers = self._handlers
            self._handlers = []
            pad_probes = self._pad_probes
            self._pad_probes = []
            self._watched_pads.clear()
        for element, handler in handlers:
            self._disconnect_signal(element, handler)
        for pad, probe_id in pad_probes:
            self._remove_probe(pad, probe_id)
        return milestones

    def _source_setup(self, _playbin: Any, source: Any) -> None:
        self._mark("source_setup_at")
        self._watch_static_pad(source, "src", "first_source_buffer_at")
        if self._source_reads is None:
            return
        try:
            pad = source.get_static_pad("src")
            if pad is not None:
                flags = self._gst.PadProbeType
                factory = source.get_factory()
                callback = self._source_reads.callback(factory.get_name() if factory else "")
                mask = flags.BUFFER | flags.BUFFER_LIST | flags.EVENT_BOTH
                if self._add_probe(pad, "source_read_trace", mask, callback):
                    with self._lock:
                        self._source_trace_pad_attached = True
        except Exception:
            # The original startup checkpoints can still be collected.
            return

    def _source_read_snapshot(self, ended_at: float) -> SourceReadTrace | None:
        added_at = self._demuxers.get("qtdemux")
        if (
            self._source_reads is None
            or not self._source_trace_pad_attached
            or added_at is None
            or added_at > ended_at
        ):
            return None
        input_at = self._qtdemux_at.get("first_demux_input_at")
        audio_at = self._qtdemux_at.get("first_demux_audio_at")
        audio_observed = audio_at is not None and audio_at <= ended_at
        end = audio_at if audio_observed else ended_at
        input_observed = input_at is not None and input_at <= end
        return self._source_reads.snapshot(
            input_at if input_observed else added_at,
            end,
            start_reason="first_input" if input_observed else "element_added",
            end_reason="first_audio" if audio_observed else "preroll_end",
        )

    def _element_setup(self, _playbin: Any, element: Any) -> None:
        try:
            factory = element.get_factory()
            if factory is None:
                return
            name = factory.get_name()
            classes = (factory.get_metadata("klass") or "").split("/")
            if name == "typefind":
                self._watch_signal(element, "have-type", self._format_detected)
            elif "Demuxer" in classes:
                self._watch_demuxer(element, name)
            elif "Audio" in classes and "Parser" in classes:
                self._watch_static_pad(element, "sink", "first_parser_input_at")
                self._watch_static_pad(element, "src", "first_parser_output_at")
            elif "Audio" in classes and "Decoder" in classes:
                self._watch_static_pad(element, "sink", "first_decoder_input_at", factory=name)
                self._watch_static_pad(element, "src", "first_decoder_output_at")
            elif is_rendering_audio_sink(element):
                self._mark("audio_sink_added_at")
                self._watch_static_pad(element, "sink", "first_sink_buffer_at")
        except Exception:
            # A diagnostic hook may be unavailable for an element or binding.
            return

    def _format_detected(self, _element: Any, _probability: int, _caps: Any) -> None:
        self._mark("format_detected_at")

    def _watch_demuxer(self, element: Any, factory: str) -> None:
        with self._lock:
            if self._result is not None:
                return
            if factory not in self._demuxers:
                self._demuxers[factory] = self._clock()
        self._watch_static_pad(element, "sink", "first_demux_input_at", factory=factory)

        def watch_output(_element: Any, pad: Any) -> None:
            if pad.get_direction() == self._gst.PadDirection.SRC:
                self._watch_pad(pad, "first_demux_output_at", factory=factory)
                self._watch_pad(pad, "first_demux_audio_at", factory=factory, audio_only=True)

        def watch_existing_pad(demuxer: Any, pad: Any, *_user_data: Any) -> bool:
            watch_output(demuxer, pad)
            return True

        # Register for dynamic pads before taking a snapshot of existing pads.
        self._watch_signal(element, "pad-added", watch_output)
        element.foreach_src_pad(watch_existing_pad, None)

    def _watch_signal(self, element: Any, signal: str, callback: Callable[..., None]) -> None:
        try:
            handler = element.connect(signal, callback)
        except Exception:
            return
        with self._lock:
            if self._result is None:
                self._handlers.append((element, handler))
                return
        self._disconnect_signal(element, handler)

    def _watch_static_pad(
        self, element: Any, pad_name: str, milestone: str, *, factory: str | None = None
    ) -> None:
        try:
            pad = element.get_static_pad(pad_name)
        except Exception:
            return
        if pad is not None:
            self._watch_pad(pad, milestone, factory=factory)

    def _watch_pad(
        self, pad: Any, milestone: str, *, factory: str | None = None, audio_only: bool = False
    ) -> None:
        seen_buffer = False

        def on_buffer(pad: Any, _info: Any, *_user_data: Any) -> Any:
            nonlocal seen_buffer
            if not seen_buffer and (not audio_only or self._is_audio_pad(pad)):
                seen_buffer = True
                self._mark(milestone, factory=factory)
            return self._gst.PadProbeReturn.OK

        flags = self._gst.PadProbeType
        self._add_probe(pad, milestone, flags.BUFFER | flags.BUFFER_LIST, on_buffer)

    def _add_probe(self, pad: Any, purpose: str, flags: Any, callback: Callable[..., Any]) -> bool:
        try:
            with self._lock:
                key = (pad, purpose)
                if self._result is not None or key in self._watched_pads:
                    return False
                self._watched_pads.add(key)
            probe_id = pad.add_probe(flags, callback)
        except Exception:
            return False
        if not probe_id:
            return False
        with self._lock:
            if self._result is None:
                self._pad_probes.append((pad, probe_id))
                return True
        # A streaming callback may register a probe while the playback thread finishes.
        self._remove_probe(pad, probe_id)
        return False

    def _is_audio_pad(self, pad: Any) -> bool:
        if _has_audio_caps(pad):
            return True
        try:
            if pad.get_name().startswith(("audio_", "audio-")):
                return True
            stream = pad.get_stream()
            return stream is not None and bool(
                stream.get_stream_type() & self._gst.StreamType.AUDIO
            )
        except Exception:
            return False

    def _mark(self, milestone: str, *, factory: str | None = None) -> None:
        with self._lock:
            if self._result is not None:
                return
            trace_qtdemux = (
                self._source_reads is not None
                and factory == "qtdemux"
                and milestone in {"first_demux_input_at", "first_demux_audio_at"}
                and milestone not in self._qtdemux_at
            )
            if milestone in self._milestones and not trace_qtdemux:
                return
            when = self._clock()
            if trace_qtdemux:
                self._qtdemux_at[milestone] = when
            if milestone not in self._milestones:
                self._milestones[milestone] = when
                if factory is not None:
                    self._milestone_factories[milestone] = factory

    @staticmethod
    def _disconnect_signal(element: Any, handler: int) -> None:
        try:
            element.disconnect(handler)
        except Exception:
            pass

    @staticmethod
    def _remove_probe(pad: Any, probe_id: int) -> None:
        try:
            pad.remove_probe(probe_id)
        except Exception:
            pass


def _has_audio_caps(pad: Any) -> bool:
    try:
        caps = pad.get_current_caps()
        return (
            caps is not None
            and caps.get_size() > 0
            and caps.get_structure(0).get_name().startswith("audio/")
        )
    except Exception:
        return False
