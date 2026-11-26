"""Display the playback route and feedback for an audio test."""

import time
from html import escape

from .admin_layout import TEMPLATE_DIR, render_admin_page
from .player_control import WebPlayerStatus
from .setup_views import render_csrf_input


PAGE = (TEMPLATE_DIR / "audio_output.html").read_text(encoding="utf-8")
TEST_MESSAGES = {
    "queued": "Waiting for the player to run the test…",
    "applied": "Test sound played.",
    "busy": "Pause playback and wait for any other test to finish.",
    "muted": "Unmute the player before running the test.",
    "disconnected": "Connect a Bluetooth audio device first.",
    "audio_error": "The output could not play the sound. Check the connection and try again.",
    "rejected": "The player could not run this test.",
}


def render_audio_output(
    status: WebPlayerStatus, bluetooth_message: str, csrf: str, request_id: str = "", outcome: str = "",
) -> str:
    recent = 0 < status.updated_at and time.time() - status.updated_at <= 10
    content = PAGE.format(
        audio_output=audio_output_label(status.audio_output) if recent else "Player status unavailable",
        bluetooth=escape(bluetooth_message),
        csrf=render_csrf_input(csrf),
        result=TEST_MESSAGES.get(outcome, ""),
        pending_id=escape(request_id, quote=True) if outcome == "queued" else "",
    )
    return render_admin_page("Audio output · Chapter", "connections", content, parent=("Connections", "/?tab=connections"))


def audio_output_label(output: str) -> str:
    return {"speaker": "Built-in speaker", "bluetooth": "Bluetooth"}.get(output, "Idle")
