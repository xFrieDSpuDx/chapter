"""Discover speakers using BlueZ objects, and release only Chapter's discovery session."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from threading import Event

from .bluetooth_audio import (
    DEFAULT_PAIR_SCAN_SECONDS,
    audio_device_name,
    is_mac_address,
    set_bluetooth_power,
)
from .bluetooth_commands import check_cancelled, run_bluetoothctl


AUDIO_SINK = "0000110b-0000-1000-8000-00805f9b34fb"


@dataclass(frozen=True)
class Speaker:
    address: str
    name: str
    paired: bool = False
    connected: bool = False


class SpeakerError(Exception):
    def __init__(self, stage: str, detail: str):
        super().__init__(detail)
        self.stage = stage


class BluezDiscovery:
    def __init__(self):
        # Gio is installed with python3-gi on the Pi. Keep the rest of Chapter usable without Bluetooth.
        from gi.repository import Gio
        self.gio = Gio
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        adapters = [path for path, interfaces in self.objects().items()
                    if interfaces.get("org.bluez.Adapter1", {}).get("Powered")]
        if not adapters:
            raise SpeakerError("Discovery", "No powered Bluetooth adapter")
        self.adapter = sorted(adapters)[0]
        self._cached_paths = None
        self._seen = set()

    def call(self, path, interface, method):
        return self.bus.call_sync("org.bluez", path, interface, method, None, None,
                                  self.gio.DBusCallFlags.NONE, 4000, None)

    def objects(self):
        return self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects").unpack()[0]

    def start(self):
        self.call(self.adapter, "org.bluez.Adapter1", "StartDiscovery")

    def stop(self):
        self.call(self.adapter, "org.bluez.Adapter1", "StopDiscovery")

    def speakers(self):
        devices = []
        objects = self.objects()
        if self._cached_paths is None:
            self._cached_paths = set(objects)
        for path, interfaces in objects.items():
            properties = interfaces.get("org.bluez.Device1")
            if properties is None or not path.startswith(self.adapter + "/"):
                continue
            if path not in self._cached_paths or "RSSI" in properties:
                self._seen.add(path)
            device = speaker_from_properties(properties, discovered=path in self._seen)
            if device is not None:
                devices.append(device)
        return tuple(sorted(devices, key=lambda device: (not device.paired, device.name.casefold(), device.address)))


def speaker_from_properties(props: dict, *, discovered: bool = False) -> Speaker | None:
    address = props.get("Address", "").upper()
    if not is_mac_address(address):
        return None
    name = props.get("Alias") or props.get("Name") or address
    paired = bool(props.get("Paired") or props.get("Bonded"))
    connected = bool(props.get("Connected"))
    uuids = {uuid.lower() for uuid in props.get("UUIDs", [])}
    audio = (AUDIO_SINK in uuids or props.get("Icon") in ("audio-card", "audio-headset", "audio-headphones", "audio-speakers")
             or (props.get("Class", 0) & 0x1f00) == 0x0400 or audio_device_name(name))
    # An advertising speaker can acquire its service UUID and name after its first sighting.
    if not audio and (uuids or props.get("Icon") or is_mac_address(name.replace("-", ":"))):
        return None
    if not (paired or connected or discovered or "RSSI" in props):
        return None
    return Speaker(address, name, paired, connected)


def discover_speakers(publish: Callable, cancel: Event, *, backend=BluezDiscovery,
                      power=None, seconds=DEFAULT_PAIR_SCAN_SECONDS) -> None:
    check_cancelled(cancel)
    if power is None:
        power = partial(set_bluetooth_power, runner=partial(run_bluetoothctl, cancel=cancel))
    enabled = power(True)
    if not enabled.success:
        raise SpeakerError("Power", enabled.message)
    if cancel.is_set():
        return
    discovery = backend()
    previous = discovery.speakers()
    publish(previous)  # Remembered speakers are selectable even when the radio scan fails.
    if cancel.is_set():
        return
    discovery.start()
    try:
        end = time.monotonic() + seconds
        while not cancel.is_set():
            devices = discovery.speakers()
            if devices != previous:
                publish(devices)
                previous = devices
            remaining = end - time.monotonic()
            if remaining <= 0 or cancel.wait(min(0.4, remaining)):
                break
    finally:
        discovery.stop()
