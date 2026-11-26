"""Connect the selected Bluetooth speaker and verify its own playback endpoint."""

from __future__ import annotations

import shutil
import subprocess
import time
from functools import partial
from threading import Event

from .bluetooth_audio import connected_info
from .bluetooth_commands import (
    CommandResult,
    check_cancelled,
    run_bluetoothctl,
    run_bluetoothctl_with_agent,
)
from .bluetooth_speakers import AUDIO_SINK, Speaker, SpeakerError


def check_result(result: CommandResult, stage: str) -> None:
    lines = result.output.splitlines()
    error = next((line for line in lines if "failed" in line.lower() or "org.bluez.Error" in line), "")
    if result.returncode or error:
        raise SpeakerError(stage, error or next(reversed(lines), "No response"))


def playback_endpoint_ready(address: str) -> bool:
    program = shutil.which("bluealsactl") or shutil.which("bluealsa-cli")
    if not program:
        raise SpeakerError("Audio output", "BlueALSA control utility unavailable")
    try:
        result = subprocess.run([program, "list-pcms"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SpeakerError("Audio output", str(error)) from error
    check_result(CommandResult(result.returncode, result.stderr), "Audio output")
    suffix = "/dev_" + address.replace(":", "_").lower() + "/a2dpsrc/sink"
    return any(path.strip().lower().endswith(suffix) for path in result.stdout.splitlines())


def connect_speaker(speaker: Speaker, progress, *, cancel: Event | None = None, runner=None, pair=None,
                    ready=playback_endpoint_ready, audio_wait=5.0) -> None:
    cancel = cancel if cancel is not None else Event()
    runner = runner or partial(run_bluetoothctl, cancel=cancel)
    pair = pair or partial(run_bluetoothctl_with_agent, cancel=cancel)
    address = speaker.address
    stage = "Connection" if speaker.paired else "Pairing"

    def info():
        check_cancelled(cancel)
        result = runner(["info", address], 4)
        check_cancelled(cancel)
        check_result(result, stage)
        return result.output

    progress(stage)
    state = info()
    if "Paired: yes" not in state and "Bonded: yes" not in state:
        if speaker.paired:
            raise SpeakerError("Connection", "Pairing was removed. Scan again.")
        check_result(pair(["pair", address], 25), "Pairing")
        state = info()
        if "Paired: yes" not in state and "Bonded: yes" not in state:
            raise SpeakerError("Pairing", "Speaker did not confirm pairing")

    if "Trusted: yes" not in state:
        progress("Trust")
        check_cancelled(cancel)
        check_result(runner(["trust", address], 5), "Trust")

    stage = "Connection"
    progress(stage)
    check_cancelled(cancel)
    # Pair often connects the audio profile itself. A generic connected flag can mean only a control link.
    if not connected_info(state) or not ready(address):
        check_cancelled(cancel)
        response = runner(["connect", address, AUDIO_SINK], 15)
        if not any(status in response.output.lower() for status in ("alreadyconnected", "org.bluez.error.inprogress")):
            check_result(response, stage)

    progress("Audio output")
    deadline = time.monotonic() + audio_wait
    while True:
        check_cancelled(cancel)
        if ready(address):
            check_cancelled(cancel)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SpeakerError("Audio output", "Speaker connected, but no A2DP playback endpoint")
        cancel.wait(min(0.3, remaining))


def disconnect_speaker(speaker: Speaker, progress, *, cancel: Event | None = None, runner=None) -> None:
    runner = runner or partial(run_bluetoothctl, cancel=cancel)
    progress("Disconnect")
    check_cancelled(cancel)
    check_result(runner(["disconnect", speaker.address], 10), "Disconnect")
