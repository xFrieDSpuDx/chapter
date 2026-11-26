"""Run bluetoothctl commands and reap the child when the caller cancels or times out."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Sequence
from concurrent.futures import CancelledError
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str = ""


def check_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError()


def run_bluetoothctl(
    args: Sequence[str], timeout: float = 5.0, *, cancel: threading.Event | None = None,
) -> CommandResult:
    check_cancelled(cancel)
    try:
        child = subprocess.Popen(["bluetoothctl", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        return CommandResult(127, "bluetoothctl is not installed")

    deadline = time.monotonic() + timeout
    with child:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (cancel is not None and cancel.is_set()):
                child.kill()
                stdout, stderr = child.communicate()
                check_cancelled(cancel)
                return CommandResult(124, combine_output(stdout, stderr) or "bluetoothctl timed out")
            try:
                interval = min(remaining, 0.1) if cancel is not None else remaining
                stdout, stderr = child.communicate(timeout=interval)
            except subprocess.TimeoutExpired:
                continue
            check_cancelled(cancel)
            return CommandResult(child.returncode, combine_output(stdout, stderr))


def combine_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    parts = []
    for value in (stdout, stderr):
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        if value:
            parts.append(value.strip())
    return "\n".join(parts).strip()


def run_bluetoothctl_with_agent(
    args: Sequence[str], timeout: float = 30.0, *, cancel: threading.Event | None = None,
) -> CommandResult:
    seconds = max(1, int(timeout))
    return run_bluetoothctl(["--agent", "NoInputNoOutput", "--timeout", str(seconds), *args], seconds + 5, cancel=cancel)
