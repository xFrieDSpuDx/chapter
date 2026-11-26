"""Serialize web player status and stream changes to an authorized browser."""

import json
import time
from collections.abc import Callable
from typing import BinaryIO

from .player_control import WebPlayerStatus
from .setup_views import player_state_label, render_time_info
from .web_artwork import artwork_url


PLAYER_EVENT_POLL_SECONDS = 0.25
PLAYER_EVENT_TIME_SECONDS = 1.0
PLAYER_EVENT_HEARTBEAT_SECONDS = 5.0


def player_status_payload(status: WebPlayerStatus) -> dict:
    volume = min(max(status.volume_percent, 0), 100)
    return {
        "hasSession": status.has_session,
        "openingKey": status.opening_key,
        "title": status.title,
        "coverUrl": artwork_url(status.cover_item_id) if status.has_session else "",
        "state": player_state_label(status),
        "timeInfo": render_time_info(status),
        "currentTime": max(0, status.current_time),
        "duration": max(0, status.duration),
        "updatedAt": status.updated_at,
        "displayDrawnAt": status.display_drawn_at,
        "isPlaying": status.is_playing,
        "volumePercent": volume,
        "playbackRate": status.playback_rate,
    }


def player_status_event_signature(payload: dict) -> str:
    important = {
        "hasSession": payload.get("hasSession"),
        "openingKey": payload.get("openingKey"),
        "title": payload.get("title"),
        "coverUrl": payload.get("coverUrl"),
        "state": payload.get("state"),
        "isPlaying": payload.get("isPlaying"),
        "volumePercent": payload.get("volumePercent"),
        "playbackRate": payload.get("playbackRate"),
        "duration": payload.get("duration"),
    }
    return json.dumps(important, sort_keys=True, separators=(",", ":"))


def player_status_sse(payload_json: str) -> bytes:
    return f"data: {payload_json}\n\n".encode("utf-8")


def stream_player_events(output: BinaryIO, read: Callable[[], WebPlayerStatus], authorized: Callable[[], bool]) -> None:
    """Check access before each update, including heartbeats on an idle player."""
    def send(event: bytes) -> bool:
        try:
            output.write(event)
            output.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return False

    if not send(b"retry: 2000\n\n"):
        return
    last_payload = last_signature = ""
    last_update = 0.0
    last_write = time.monotonic()
    while True:
        payload = player_status_payload(read())
        encoded = json.dumps(payload, separators=(",", ":"))
        signature = player_status_event_signature(payload)
        now = time.monotonic()
        changed = signature != last_signature or (encoded != last_payload and now - last_update >= PLAYER_EVENT_TIME_SECONDS)
        if changed or now - last_write >= PLAYER_EVENT_HEARTBEAT_SECONDS:
            if not authorized():
                return
            if changed:
                if not send(player_status_sse(encoded)):
                    return
                last_payload, last_signature = encoded, signature
                last_update = now
            elif not send(b": heartbeat\n\n"):
                return
            last_write = now
        time.sleep(PLAYER_EVENT_POLL_SECONDS)
