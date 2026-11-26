"""Find an opportunity to seek to the resume position during preroll."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


PREROLL_POLL_SECONDS = 0.025


def can_seek_to(gst: Any, playbin: Any, seconds: float) -> bool:
    try:
        # Wait until playbin has an audio stream to route the query through.
        if playbin.get_property("n-audio") < 1:
            return False
        query = gst.Query.new_seeking(gst.Format.TIME)
        if not playbin.query(query):
            return False
        format_, seekable, start, end = query.parse_seeking()
        position = int(seconds * gst.SECOND)
        return (
            format_ == gst.Format.TIME
            and seekable
            and start <= position
            and (end < 0 or position <= end)
        )
    except Exception:
        return False


def wait_for_resume_seek(
    wait_for_state: Callable[[float], str],
    try_seek: Callable[[], bool | None] | None,
    *,
    clock: Callable[[], float],
    timeout: float,
    cancelled: Callable[[], bool] | None,
) -> str:
    """Watch the transition until a seek starts or the normal preroll wait ends.

    The seek callback returns None when the pipeline needs more time, or the
    outcome of sending a seek. A rejected seek is retried after preroll.
    """
    deadline = clock() + timeout
    interval = 0.0
    seek_rejected = False
    while True:
        if cancelled is not None and cancelled():
            return "cancelled"
        wait = wait_for_state(interval)
        if wait != "timeout":
            return wait
        if cancelled is not None and cancelled():
            return "cancelled"
        if clock() >= deadline:
            return "timeout"
        if not seek_rejected and try_seek is not None:
            accepted = try_seek()
            if accepted is True:
                return "seek_started"
            seek_rejected = accepted is False
        remaining = deadline - clock()
        if remaining <= 0:
            return "timeout"
        interval = min(PREROLL_POLL_SECONDS, remaining)
