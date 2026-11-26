"""Store a bounded history of anonymous playback startup timings."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .build_info import installed_build
from .player_state import state_path
from .storage import write_json_atomically


SAMPLES_PER_SOURCE = 6
STARTUP_SOURCES = ("download", "audiobookshelf", "podcast", "unknown")
STARTUP_ACTIONS = ("start", "continue")
STARTUP_OUTPUTS = ("default", "bluetooth")
STARTUP_OUTCOMES = (
    "output_observed", "output_timeout", "output_interrupted", "sink_stats_unavailable",
    "muted", "preparation_failed", "gstreamer_failed",
)
STARTUP_DURATION_FIELDS = (
    "input_ms", "prepare_ms", "audiobookshelf_ms", "owner_wait_ms", "gst_bindings_ms",
    "gst_init_ms", "gst_runtime_wait_ms", "gst_preroll_ms", "gst_seek_ms", "gst_ms", "gst_to_output_ms",
    "select_to_output_ms",
)
_HISTORY_LOCK = threading.Lock()


@dataclass(frozen=True)
class StartupSample:
    recorded_at: float
    source: str
    action: str
    outcome: str
    durations_ms: dict[str, int]
    output: str | None = None
    build_id: str | None = None
    first_pipeline_since_restart: bool | None = None


def _parse_sample(data: object) -> StartupSample | None:
    if not isinstance(data, dict):
        return None
    recorded_at = data.get("recorded_at")
    source, action, outcome = data.get("source"), data.get("action"), data.get("outcome")
    durations = data.get("durations_ms")
    if type(recorded_at) not in (int, float) or not 0 < recorded_at < 253_402_300_800:
        return None
    if source not in STARTUP_SOURCES or action not in STARTUP_ACTIONS or outcome not in STARTUP_OUTCOMES:
        return None
    if not isinstance(durations, dict):
        return None
    output = data.get("output")
    build_id = data.get("build_id")
    first_pipeline = data.get("first_pipeline_since_restart")
    return StartupSample(
        recorded_at, source, action, outcome,
        {key: durations[key] for key in STARTUP_DURATION_FIELDS
         if type(durations.get(key)) is int and 0 <= durations[key] < 1_000_000_000},
        output=output if output in STARTUP_OUTPUTS else None,
        build_id=build_id if isinstance(build_id, str) and re.fullmatch(r"[0-9a-f]{12}", build_id) else None,
        first_pipeline_since_restart=first_pipeline if isinstance(first_pipeline, bool) else None,
    )


def _limit_history(samples: list[StartupSample]) -> list[StartupSample]:
    counts = dict.fromkeys(STARTUP_SOURCES, 0)
    first_plays = set()
    recent = []
    for sample in samples:
        # Keep the latest measured first play even after a long stretch of normal listening.
        first_play = sample.first_pipeline_since_restart is True and sample.outcome == "output_observed"
        if counts[sample.source] < SAMPLES_PER_SOURCE or (first_play and sample.source not in first_plays):
            recent.append(sample)
            counts[sample.source] += 1
            if first_play:
                first_plays.add(sample.source)
    return recent


def startup_history_path() -> Path:
    return state_path().with_name("playback-startup-history.json")


def load_startup_history() -> list[StartupSample]:
    try:
        rows = json.loads(startup_history_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return _limit_history([sample for row in rows if (sample := _parse_sample(row)) is not None])


def record_startup_sample(sample: StartupSample) -> None:
    sample = _parse_sample(asdict(replace(sample, build_id=installed_build().build_id)))
    if sample is None:
        return
    with _HISTORY_LOCK:
        recent = _limit_history([sample, *load_startup_history()])
        try:
            write_json_atomically(startup_history_path(), [asdict(row) for row in recent])
        except OSError as error:
            print(f"Could not save playback timings: {type(error).__name__}")
