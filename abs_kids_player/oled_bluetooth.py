"""Apply Bluetooth discovery and connection results to the OLED menu and playback output."""

import queue
import threading
import traceback
from dataclasses import dataclass
from typing import Callable

from .audio_output_test import AudioOutputTest
from .bluetooth_audio import (
    BluetoothActionResult,
    bluetooth_powered,
    connected_bluetooth_audio_device,
    set_bluetooth_power,
)
from .display_frames import DisplayFrame, TwoLineFrame
from .playback_owner import PlaybackOwner
from .rotary_ui import ApplianceMenu, CommandType, MenuCommand, Screen


@dataclass(frozen=True)
class BluetoothStatusResult:
    enabled: bool
    connected: bool


def bluetooth_result_frame(result: BluetoothActionResult) -> DisplayFrame:
    if result.success and result.connected is False:
        return TwoLineFrame(
            "Disconnected" if result.device_name else "Bluetooth off", "Internal speaker"
        )
    if result.success and result.device_name:
        return TwoLineFrame("Speaker ready", result.device_name)
    if result.success:
        return TwoLineFrame("Bluetooth", "On" if result.enabled else "Off")
    return TwoLineFrame("Bluetooth failed", result.message)


def bluetooth_status_result() -> BluetoothStatusResult:
    enabled = bluetooth_powered()
    connected = connected_bluetooth_audio_device() is not None if enabled else False
    return BluetoothStatusResult(enabled=enabled, connected=connected)


def start_bluetooth_status_worker(
    results: queue.SimpleQueue[BluetoothStatusResult],
    status_loader: Callable[[], BluetoothStatusResult] = bluetooth_status_result,
) -> None:
    def run() -> None:
        try:
            results.put(status_loader())
        except Exception:
            print("Bluetooth startup status worker failed:")
            traceback.print_exc()

    threading.Thread(target=run, daemon=True).start()


def start_bluetooth_power_worker(
    enabled: bool,
    bluetooth_results: queue.SimpleQueue[BluetoothActionResult],
) -> None:
    def run() -> None:
        try:
            bluetooth_results.put(set_bluetooth_power(enabled))
        except Exception:
            print("Bluetooth power worker failed:")
            traceback.print_exc()
            bluetooth_results.put(BluetoothActionResult(False, "Bluetooth error", enabled=None))

    threading.Thread(target=run, daemon=True).start()


class OledBluetooth:
    def __init__(self) -> None:
        self.enabled = False
        self.connected = False
        self.used = False
        self.status_results: queue.SimpleQueue[BluetoothStatusResult] = queue.SimpleQueue()
        self.results: queue.SimpleQueue[BluetoothActionResult] = queue.SimpleQueue()

    def apply_startup_status(
        self, menu: ApplianceMenu, playback: PlaybackOwner, commands: list[MenuCommand]
    ) -> bool:
        self.used = (
            self.used
            or menu.speakers.active
            or any(command.type == CommandType.SET_BLUETOOTH_ENABLED for command in commands)
        )
        changed = False
        while True:
            try:
                status = self.status_results.get_nowait()
            except queue.Empty:
                return changed
            if self.used:
                continue  # A startup probe can finish after the listener has changed the adapter.
            self.enabled, self.connected = status.enabled, status.connected
            menu.set_bluetooth_enabled(self.enabled)
            menu.set_bluetooth_connected(self.connected)
            playback.set_bluetooth_output_enabled(self.connected)
            playback.refresh_audio_output()
            changed = True

    def poll_picker(self, menu: ApplianceMenu) -> None:
        if menu.screen == Screen.BLUETOOTH_DEVICES:
            result = menu.speakers.poll()
            if menu.speakers.powered:
                self.enabled = True
                menu.set_bluetooth_enabled(True)
            if result is not None:
                self.results.put(result)
        elif menu.speakers.active:
            menu.speakers.close()

    def apply_actions(
        self,
        menu: ApplianceMenu,
        playback: PlaybackOwner,
        audio_test: AudioOutputTest,
    ) -> DisplayFrame | None:
        overlay = None
        while True:
            try:
                result = self.results.get_nowait()
            except queue.Empty:
                return overlay
            if result.enabled is not None:
                self.enabled = result.enabled
            if result.connected is not None:
                self.connected = result.connected
            elif result.enabled is False:
                self.connected = False
            menu.set_bluetooth_enabled(self.enabled)
            menu.set_bluetooth_connected(self.connected)
            if result.success:
                if result.device_address and result.connected:
                    audio_test.bluetooth_address = result.device_address
                    playback.set_bluetooth_output_enabled(True, result.device_address)
                else:
                    if not self.connected:
                        audio_test.bluetooth_address = ""
                    playback.set_bluetooth_output_enabled(self.connected)
                playback.refresh_audio_output()
            if result.device_address and menu.screen == Screen.BLUETOOTH_DEVICES:
                menu.close_bluetooth_menu()
            overlay = bluetooth_result_frame(result)
