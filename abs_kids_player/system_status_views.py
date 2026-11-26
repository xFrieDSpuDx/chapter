"""Present the local diagnostic measurements in Chapter's System view."""

from __future__ import annotations

from datetime import datetime
from html import escape

from .admin_layout import TEMPLATE_DIR, render_admin_page
from .playback_startup_history import StartupSample
from .system_diagnostics import SystemDiagnostics


SYSTEM_STATUS_PAGE = (TEMPLATE_DIR / "system_status.html").read_text(encoding="utf-8")

PLAYBACK_SOURCES = {
    "download": "Downloaded",
    "audiobookshelf": "Audiobookshelf stream",
    "podcast": "Podcast",
    "unknown": "Unknown source",
}
PLAYBACK_OUTCOMES = {
    "output_observed": "Audio observed",
    "output_timeout": "Timed out waiting for audio",
    "output_interrupted": "Interrupted",
    "sink_stats_unavailable": "Audio timing unavailable",
    "muted": "Muted",
    "preparation_failed": "Could not prepare playback",
    "gstreamer_failed": "Could not start audio",
}
LIBRARY_RESULTS = {"succeeded": "Succeeded", "failed": "Failed", "not_recorded": "Not recorded yet"}
PROGRESS_RESULTS = {
    "sent": "Synced to Audiobookshelf",
    "server_newer": "Kept the newer position from Audiobookshelf",
    "retrying": "Waiting to retry",
    "not_recorded": "Not recorded yet",
}


def format_timestamp(timestamp: float | None) -> str:
    if timestamp:
        try:
            return datetime.fromtimestamp(timestamp).astimezone().strftime("%d %b %Y, %H:%M:%S %Z")
        except (OSError, ValueError, OverflowError):
            pass
    return "Not recorded yet"


def format_storage(size: int | None) -> str:
    if size is None:
        return "Unavailable"
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f} GiB"
    return f"{size / 1024 ** 2:.1f} MiB"


def format_duration(sample: StartupSample, field: str) -> str:
    value = sample.durations_ms.get(field)
    return "—" if value is None else f"{value / 1000:.2f} s"


def render_startup_history(samples: list[StartupSample]) -> str:
    if not samples:
        return "<p>Start or continue a book to record its startup timings.</p>"
    rows = []
    for sample in samples:
        first_audio = format_duration(sample, "select_to_output_ms") if sample.outcome == "output_observed" else "—"
        source = PLAYBACK_SOURCES.get(sample.source, "Unknown source")
        outcome = PLAYBACK_OUTCOMES.get(sample.outcome, "Unknown result")
        if sample.first_pipeline_since_restart is not None:
            outcome += " · First play after restart" if sample.first_pipeline_since_restart else " · Later play"
        entries = (
            ("First audio", first_audio),
            ("Preparation", format_duration(sample, "prepare_ms")),
            ("Preroll", format_duration(sample, "gst_preroll_ms")),
            ("Build", sample.build_id or "—"),
        )
        metrics = "".join(f"<div><dt>{label}</dt><dd>{escape(value)}</dd></div>" for label, value in entries)
        rows.append(
            f'<li><div class="record-title"><strong>{escape(source)} · {escape(sample.action.capitalize())}</strong>'
            f'<p class="muted">{escape(format_timestamp(sample.recorded_at))}</p>'
            f'<p class="muted">{escape(outcome)}</p></div><dl class="startup-metrics">{metrics}</dl></li>'
        )
    return (
        '<p class="hint">First audio is measured from selection to the first observed output.</p>'
        '<ul class="record-list row-dividers startup-results">' + "".join(rows) + "</ul>"
    )


def render_system_status(report: SystemDiagnostics) -> str:
    content = SYSTEM_STATUS_PAGE.format(
        version=escape(report.build.version),
        build_id=escape(report.build.build_id or "unavailable"),
        last_library_refresh=escape(format_timestamp(report.last_library_refresh_at)),
        library_result=LIBRARY_RESULTS[report.library_refresh_result],
        last_progress_sync=escape(format_timestamp(report.progress_sync.last_success_at)),
        progress_result=PROGRESS_RESULTS[report.progress_sync.outcome],
        pending_count=report.pending_progress_count,
        storage_free=format_storage(report.storage.free_bytes),
        storage_used=format_storage(report.storage.download_bytes),
        storage_limit=format_storage(report.storage.download_limit_bytes),
        playback_history=render_startup_history(report.playback_startups),
    )
    return render_admin_page("System status · Chapter", "system", content, parent=("System", "/?tab=system"))
