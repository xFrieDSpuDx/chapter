"""Play a short, low-level confirmation sound through a selected ALSA output."""

from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path

from .audio import ensure_boot_chime_wav
from .bluetooth_audio import preferred_bluetooth_alsa_device
from .player_control import WebPlayerCommand, save_command_result


AUDIO_TEST_COMMAND = "audio_output_test"
AUDIO_TEST_OUTPUTS = {"speaker": 1, "bluetooth": 2}
AUDIO_TEST_MAX_PERCENT = 8


def play_output_test(output: int, percent: int, address: str = "") -> str:
    try:
        device = "default"
        if output == AUDIO_TEST_OUTPUTS["bluetooth"]:
            bluetooth_device = preferred_bluetooth_alsa_device(address)
            if not bluetooth_device:
                return "disconnected"
            device = f"plug:{bluetooth_device}"

        with tempfile.TemporaryDirectory(prefix="chapter-audio-test-") as temp_dir:
            wav = Path(temp_dir) / "test.wav"
            ensure_boot_chime_wav(wav, percent)
            subprocess.run(
                ["aplay", "-q", "-D", device, str(wav)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=True, timeout=5,
            )
    except (OSError, subprocess.SubprocessError):
        return "audio_error"
    return "applied"


class AudioOutputTest:
    def __init__(self) -> None:
        self.worker: threading.Thread | None = None
        self.bluetooth_address = ""

    def request(
        self,
        command: WebPlayerCommand,
        *,
        playback_busy: bool,
        volume_percent: int,
        muted: bool,
        night_limit: int,
    ) -> None:
        output = command.value
        if output is None or output not in AUDIO_TEST_OUTPUTS.values():
            save_command_result(command, "rejected")
            return
        if playback_busy or (self.worker is not None and self.worker.is_alive()):
            save_command_result(command, "busy")
            return
        percent = min(volume_percent, night_limit, AUDIO_TEST_MAX_PERCENT)
        if muted or percent <= 0:
            save_command_result(command, "muted")
            return

        address = self.bluetooth_address

        def play() -> None:
            save_command_result(command, play_output_test(output, percent, address))

        self.worker = threading.Thread(target=play, name="chapter-audio-test", daemon=True)
        self.worker.start()
