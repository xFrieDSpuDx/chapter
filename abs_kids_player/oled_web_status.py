"""Publish the device's current playback and display heartbeat for the web interface."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from .playback_owner import PlaybackOwner, PlaybackSnapshot
from .player_control import WebPlayerStatus


def web_status_for_playback(
    playback: PlaybackOwner,
    volume_percent: int,
    muted: bool,
) -> WebPlayerStatus:
    status = web_status_for_playback_state(playback.state, volume_percent, muted)
    return replace(status, opening_key=playback.opening_key)


def web_status_for_playback_state(
    state: PlaybackSnapshot,
    volume_percent: int,
    muted: bool,
) -> WebPlayerStatus:
    if state.session is None:
        return WebPlayerStatus(volume_percent=volume_percent, muted=muted or volume_percent <= 0)
    return WebPlayerStatus(
        title=state.session.title,
        cover_item_id=state.session.library_item_id,
        author=state.session.author,
        current_time=state.current_time,
        duration=state.session.duration,
        is_playing=state.is_playing,
        audio_output="bluetooth" if state.audio_output_device else "speaker",
        volume_percent=volume_percent,
        muted=muted or volume_percent <= 0,
        session_key=state.session_key,
        account=state.session.progress_account,
        episode_id=state.session.episode_id,
        downloaded=state.session.downloaded,
        playback_rate=state.playback_rate,
        library_id=state.session.library_id,
    )


class WebStatusPublisher:
    """Publish the latest OLED state; give unavailable storage time to recover."""

    def __init__(self, save: Callable[[WebPlayerStatus], None]) -> None:
        self._save = save
        self._retry_at = 0.0

    def publish(
        self, playback: PlaybackOwner, volume_percent: int, muted: bool, *, display_drawn: bool = False,
    ) -> None:
        now = time.monotonic()
        if now < self._retry_at:
            return
        status = web_status_for_playback(playback, volume_percent, muted)
        if display_drawn:
            status = replace(status, display_drawn_at=time.time())
        try:
            self._save(status)
        except OSError as error:
            print(f"Could not publish player status: {error}")
            self._retry_at = now + 5
