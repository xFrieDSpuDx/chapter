"""State and background jobs for the OLED speaker picker."""

from __future__ import annotations

import queue
import threading
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass

from .bluetooth_audio import BluetoothActionResult
from .bluetooth_commands import check_cancelled
from .bluetooth_connection import connect_speaker, disconnect_speaker
from .bluetooth_speakers import Speaker, SpeakerError, discover_speakers
from .display_frames import BluetoothDevicesFrame


@dataclass(frozen=True)
class Choice:
    key: str
    label: str
    title: str


class SpeakerPicker:
    def __init__(self, *, discover=discover_speakers, connect=connect_speaker, disconnect=disconnect_speaker):
        self.discover = discover
        self.connect = connect
        self.disconnect = disconnect
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chapter-speakers")
        self._job: Future | None = None
        self._cancel = threading.Event()
        self._events = queue.SimpleQueue()
        self.active = False
        self.devices: tuple[Speaker, ...] = ()
        self.target: Speaker | None = None
        self.disconnecting = False
        self.powered = False
        self.stage = ""
        self.failed = False
        self.selection = "back"
        self._turned = False

    def close(self):
        self.active = False
        self._cancel.set()
        if self._job is not None:
            self._job.cancel()
            self._job = None
        # Workers publish only into their own queue, including after Back or a newer scan.
        self._events = queue.SimpleQueue()

    def shutdown(self):
        self.close()
        self._worker.shutdown(wait=True, cancel_futures=True)

    def _start_job(self, operation, stage):
        self.close()
        self.active = True
        self.stage = stage
        self.failed = False
        self._cancel = cancel = threading.Event()
        events = self._events

        def work():
            try:
                check_cancelled(cancel)
                operation(events, cancel)
            except CancelledError:
                return
            except Exception as error:
                if cancel.is_set():
                    return
                failed_stage = error.stage if isinstance(error, SpeakerError) else stage
                print(f"Bluetooth {failed_stage} failed: {error}", flush=True)
                events.put(("failure", failed_stage))

        self._job = self._worker.submit(work)

    def open(self):
        self.powered = False
        self.devices = ()
        self.target = None
        self.selection = "back"
        self._turned = False

        def scan(events, cancel):
            self.discover(lambda devices: events.put(("devices", devices)), cancel)
            events.put(("scanned", None))

        self._start_job(scan, "Discovery")

    def _choose_speaker(self, speaker, disconnect=False):
        self.target = speaker
        self.disconnecting = disconnect
        action = self.disconnect if disconnect else self.connect
        stage = "Disconnect" if disconnect else "Connection" if speaker.paired else "Pairing"
        self.selection = "back"

        def run(events, cancel):
            def progress(step):
                check_cancelled(cancel)
                events.put(("stage", step))
            action(speaker, progress, cancel=cancel)
            events.put(("finished", BluetoothActionResult(
                True, "Disconnected" if disconnect else "Speaker ready", enabled=True,
                connected=not disconnect, device_address=speaker.address, device_name=speaker.name,
            )))

        self._start_job(run, stage)

    def poll(self) -> BluetoothActionResult | None:
        while self.active:
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "devices":
                self.powered = True
                self.devices = value
                if value and ((self.selection == "back" and not self._turned) or
                              self.selection not in {row.key for row in self.choices()}):
                    self.selection = value[0].address
            elif kind == "scanned":
                self.stage = ""
                if not self.devices and not self._turned:
                    self.selection = "scan"
            elif kind == "stage":
                self.stage = value
            elif kind == "failure":
                self.stage = value
                self.failed = True
                self.selection = "retry"
            elif kind == "finished":
                self.close()
                return value
        return None

    def choices(self):
        title = self.stage
        if self.failed:
            title += " failed"
        elif self.stage == "Discovery":
            title = "Searching…"
        rows = [Choice("back", "Back", title or "Speakers")]
        if self.failed:
            rows.append(Choice("retry", "Retry", title))
        if self.target is None:
            for speaker in self.devices:
                heading = "Use speaker" if speaker.connected else "Reconnect" if speaker.paired else "Pair speaker"
                rows.append(Choice(speaker.address, speaker.name, heading))
                if speaker.connected:
                    rows.append(Choice("off/" + speaker.address, speaker.name, "Disconnect"))
            if not self.stage:
                rows.append(Choice("scan", "Search again", "Speakers" if self.devices else "No speakers found"))
        return rows

    def rotate(self, direction):
        rows = self.choices()
        index = next((i for i, row in enumerate(rows) if row.key == self.selection), 0)
        self.selection = rows[max(0, min(len(rows) - 1, index + direction))].key
        self._turned = True

    def select(self) -> bool:
        if self.selection == "back":
            self.close()
            return False
        if self.selection in ("scan", "retry"):
            if self.target:
                self._choose_speaker(self.target, self.disconnecting)
            else:
                self.open()
        else:
            for speaker in self.devices:
                if self.selection in (speaker.address, "off/" + speaker.address):
                    self._choose_speaker(speaker, self.selection.startswith("off/"))
                    break
        return True

    def frame(self):
        rows = self.choices()
        index = next((i for i, row in enumerate(rows) if row.key == self.selection), 0)
        row = rows[index]
        return BluetoothDevicesFrame(row.label, index, len(rows), heading=row.title)
