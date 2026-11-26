"""Record playback startup phases and observe the first audio sink render."""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .http_range_probe import HttpRangeProbe, HttpRangeTrace
from .models import PlaybackSession
from .playback_startup_history import STARTUP_DURATION_FIELDS, StartupSample
from .preroll_probe import PrerollMilestones, is_rendering_audio_sink


OUTPUT_WAIT_SECONDS = 2.0


@dataclass(frozen=True)
class PlaybackTrackTiming:
    number: int
    count: int
    book_resume: float
    start_offset: float
    track_seek: float
    file_id: int | None
    size_bytes: int | None
    play_method: int | None

    def log_fields(self) -> list[str]:
        fields = [
            f"track_number={self.number}",
            f"track_count={self.count}",
            f"book_resume_s={self.book_resume:.3f}",
            f"track_start_s={self.start_offset:.3f}",
            f"track_seek_s={self.track_seek:.3f}",
        ]
        for key, value in (
            ("track_file_id", self.file_id),
            ("track_size_bytes", self.size_bytes),
            ("abs_play_method", self.play_method),
        ):
            if value is not None:
                fields.append(f"{key}={value}")
        return fields


@dataclass
class PlaybackTiming:
    selected_at: float
    requested_at: float
    action: str
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    source: str = "unknown"
    source_seconds: float | None = None
    prepared_at: float | None = None
    pipeline_at: float | None = None
    pipeline_setup_seconds: dict[str, float] = field(default_factory=dict)
    preroll_at: float | None = None
    pause_returned_at: float | None = None
    preroll_finished_at: float | None = None
    preroll_wait: str | None = None
    preroll_milestones: PrerollMilestones = field(default_factory=PrerollMilestones)
    seek_at: float | None = None
    seek_finished_at: float | None = None
    seek_wait: str | None = None
    seek_mode: str | None = None
    early_seek_attempt_at: float | None = None
    playing_at: float | None = None
    gain_at: float | None = None
    startup_finished_at: float | None = None
    rendered_at: float | None = None
    output: str | None = None
    status: str | None = None
    track: PlaybackTrackTiming | None = None
    http_ranges: HttpRangeTrace | None = None
    _reported: bool = False
    startup_reporter: Callable[[StartupSample], None] | None = field(default=None, repr=False, compare=False)
    first_pipeline_since_restart: bool | None = None

    @contextmanager
    def measure_source(self, source: str) -> Iterator[None]:
        self.source = source
        started_at = self.clock()
        try:
            yield
        finally:
            self.source_seconds = self.clock() - started_at

    def observe_output(self, status: str, rendered_at: float | None = None) -> None:
        if self.status is None:
            self.status = status
            self.rendered_at = rendered_at
        if self.startup_finished_at is not None:
            self.report()

    def record_track(
        self, session: PlaybackSession, track_index: int, book_time: float, seek_seconds: float
    ) -> None:
        track = session.tracks[track_index]
        self.track = PlaybackTrackTiming(
            number=track_index + 1,
            count=len(session.tracks),
            book_resume=book_time,
            start_offset=track.start_offset,
            track_seek=seek_seconds,
            file_id=track.file_id,
            size_bytes=track.size_bytes,
            play_method=session.play_method,
        )

    def report(self) -> None:
        if self.status is None or self._reported:
            return
        self._reported = True
        fields = [
            f"status={self.status}",
            f"source={self.source}",
            f"action={self.action}",
        ]
        if self.output is not None:
            fields.append(f"output={self.output}")
        if self.source_seconds is not None:
            fields.append(f"{self.source}_ms={self.source_seconds * 1000:.0f}")
        if self.preroll_wait is not None:
            fields.append(f"gst_preroll_wait={self.preroll_wait}")
        if self.seek_wait is not None:
            fields.append(f"gst_seek_wait={self.seek_wait}")
        if self.seek_mode is not None:
            fields.append(f"gst_seek_mode={self.seek_mode}")
        if self.track is not None:
            fields.extend(self.track.log_fields())
        if self.http_ranges is not None:
            fields.extend(self.http_ranges.log_fields())
        fields.extend(self._pipeline_setup_fields())
        preroll = self.preroll_milestones
        if preroll.demuxer_factories:
            fields.append(f"gst_demuxers={','.join(preroll.demuxer_factories)}")
        if preroll.first_decoder_input_factory:
            fields.append(f"gst_decoder_factory={preroll.first_decoder_input_factory}")
        for label, factory in (
            ("input", preroll.first_demux_input_factory),
            ("output", preroll.first_demux_output_factory),
            ("audio", preroll.first_demux_audio_factory),
        ):
            if factory is not None:
                fields.append(f"gst_demux_{label}_factory={factory}")
        phases = (
            ("input_ms", self.selected_at, self.requested_at),
            ("prepare_ms", self.requested_at, self.prepared_at),
            ("owner_wait_ms", self.prepared_at, self.pipeline_at),
            ("gst_preroll_ms", self.preroll_at, self.preroll_finished_at),
            ("gst_pause_call_ms", self.preroll_at, self.pause_returned_at),
            ("gst_preroll_wait_ms", self.pause_returned_at, self.preroll_finished_at),
            ("gst_source_setup_ms", self.preroll_at, preroll.source_setup_at),
            ("gst_first_source_buffer_ms", self.preroll_at, preroll.first_source_buffer_at),
            ("gst_format_detected_ms", self.preroll_at, preroll.format_detected_at),
            ("gst_first_demux_input_ms", self.preroll_at, preroll.first_demux_input_at),
            ("gst_first_demux_output_ms", self.preroll_at, preroll.first_demux_output_at),
            ("gst_first_demux_audio_ms", self.preroll_at, preroll.first_demux_audio_at),
            ("gst_first_parser_input_ms", self.preroll_at, preroll.first_parser_input_at),
            ("gst_first_parser_output_ms", self.preroll_at, preroll.first_parser_output_at),
            ("gst_first_decoder_input_ms", self.preroll_at, preroll.first_decoder_input_at),
            ("gst_first_decoder_output_ms", self.preroll_at, preroll.first_decoder_output_at),
            ("gst_first_decoded_audio_ms", self.preroll_at, preroll.first_decoded_audio_at),
            (
                "gst_source_to_decoded_audio_ms",
                preroll.first_source_buffer_at,
                preroll.first_decoded_audio_at,
            ),
            ("gst_audio_sink_added_ms", self.preroll_at, preroll.audio_sink_added_at),
            ("gst_first_sink_buffer_ms", self.preroll_at, preroll.first_sink_buffer_at),
            (
                "gst_after_first_sink_buffer_ms",
                preroll.first_sink_buffer_at,
                self.preroll_finished_at,
            ),
            ("gst_preroll_to_seek_ms", self.preroll_finished_at, self.seek_at),
            ("gst_early_seek_attempt_ms", self.preroll_at, self.early_seek_attempt_at),
            ("gst_seek_ms", self.seek_at, self.seek_finished_at),
            ("gst_seek_to_play_ms", self.seek_finished_at, self.playing_at),
            ("gst_play_to_gain_ms", self.playing_at, self.gain_at),
            ("gst_to_gain_ms", self.pipeline_at, self.gain_at),
            ("select_to_gain_ms", self.selected_at, self.gain_at),
            ("gst_ms", self.pipeline_at, self.startup_finished_at),
            ("gst_to_output_ms", self.pipeline_at, self.rendered_at),
            ("select_to_output_ms", self.selected_at, self.rendered_at),
        )
        durations_ms: dict[str, int] = {}
        for label, start, end in phases:
            if start is not None and end is not None and end >= start:
                elapsed = round((end - start) * 1000)
                fields.append(f"{label}={elapsed}")
                if label in STARTUP_DURATION_FIELDS:
                    durations_ms[label] = elapsed
        print("Playback timing " + " ".join(fields))
        if self.http_ranges is not None and self.preroll_at is not None:
            for line in self.http_ranges.log_lines(self.preroll_at, self.seek_at, self.playing_at):
                print(line)
        if preroll.source_reads is not None and self.preroll_at is not None:
            for line in preroll.source_reads.log_lines(self.preroll_at):
                print(line)
        if self.startup_reporter is not None:
            for step in ("bindings", "init", "runtime_wait"):
                if step in self.pipeline_setup_seconds:
                    durations_ms[f"gst_{step}_ms"] = round(self.pipeline_setup_seconds[step] * 1000)
            if self.source == "audiobookshelf" and self.source_seconds is not None:
                durations_ms["audiobookshelf_ms"] = round(self.source_seconds * 1000)
            self.startup_reporter(StartupSample(
                time.time(), self.source, self.action, self.status, durations_ms, self.output,
                first_pipeline_since_restart=self.first_pipeline_since_restart,
            ))

    def _pipeline_setup_fields(self) -> list[str]:
        fields = []
        if (
            self.pipeline_at is not None
            and self.preroll_at is not None
            and self.preroll_at >= self.pipeline_at
        ):
            total = self.preroll_at - self.pipeline_at
            fields.append(f"gst_pre_preroll_ms={total * 1000:.0f}")
            if self.pipeline_setup_seconds:
                other = max(0.0, total - sum(self.pipeline_setup_seconds.values()))
                fields.append(f"gst_pre_preroll_other_ms={other * 1000:.0f}")
        fields.extend(
            f"gst_{step}_ms={seconds * 1000:.0f}"
            for step, seconds in self.pipeline_setup_seconds.items()
        )
        return fields

    def report_startup_failure(self) -> None:
        self.status = "gstreamer_failed"
        self.rendered_at = None
        self.report()


@contextmanager
def trace_http_ranges(playbin: Any, timing: PlaybackTiming | None) -> Iterator[None]:
    if (
        timing is None
        or timing.source != "audiobookshelf"
        or os.environ.get("CHAPTER_TRACE_HTTP_RANGES") != "1"
    ):
        yield
        return
    probe = HttpRangeProbe(playbin, timing.clock)
    probe.attach()
    try:
        yield
    finally:
        timing.http_ranges = probe.finish()


@contextmanager
def trace_gstreamer_init() -> Iterator[None]:
    """Bracket native debug output when a startup trace is requested."""
    if os.environ.get("CHAPTER_TRACE_GST_INIT") != "1":
        yield
        return
    pid = os.getpid()
    print(f"GStreamer init phase=begin pid={pid}", file=sys.stderr, flush=True)
    status = "failed"
    try:
        yield
        status = "completed"
    finally:
        print(f"GStreamer init phase=end pid={pid} status={status}", file=sys.stderr, flush=True)


@contextmanager
def measure_pipeline_setup(timing: PlaybackTiming | None, step: str) -> Iterator[None]:
    """Record one startup step even if the operation raises."""
    if timing is None:
        yield
        return
    started_at = timing.clock()
    try:
        yield
    finally:
        timing.pipeline_setup_seconds[step] = timing.clock() - started_at


class AudioRenderProbe:
    """Sample the audio sink's rendered-frame counter after startup gain opens."""

    def __init__(self, timing: PlaybackTiming, sink: Any) -> None:
        self.timing = timing
        self._sink = sink
        self._initial_count: int | None = None

    def gain_applied(self) -> None:
        if self.timing.gain_at is None:
            self.timing.gain_at = self.timing.clock()
            self._initial_count = rendered_count(self._sink)
            if self._initial_count is None:
                self.timing.observe_output("sink_stats_unavailable")
        else:
            self.poll()

    def poll(self) -> None:
        if self.timing.status is not None or self._initial_count is None:
            return
        count = rendered_count(self._sink)
        now = self.timing.clock()
        if count is None:
            self.timing.observe_output("sink_stats_unavailable")
        elif count > self._initial_count:
            self.timing.observe_output("output_observed", rendered_at=now)
        elif now - self.timing.gain_at >= OUTPUT_WAIT_SECONDS:
            self.timing.observe_output("output_timeout")


def find_audio_sink(gst: Any, playbin: Any) -> Any:
    # Recurse through the default autoaudiosink bin as well as explicit ALSA sinks.
    try:
        children = playbin.iterate_recurse()
        resynced = False
        while True:
            result, element = children.next()
            if result == gst.IteratorResult.RESYNC and not resynced:
                children.resync()
                resynced = True
                continue
            if result != gst.IteratorResult.OK:
                return None
            if is_rendering_audio_sink(element):
                return element
    except Exception:
        # An unavailable counter must never prevent playback.
        return None


def rendered_count(sink: Any) -> int | None:
    if sink is None:
        return None
    try:
        stats = sink.get_property("stats")
        return int(stats.get_value("rendered"))
    except Exception:
        return None
