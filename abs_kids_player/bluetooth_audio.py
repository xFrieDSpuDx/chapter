from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from .bluetooth_commands import (
    CommandResult,
    combine_output,
    run_bluetoothctl,
    run_bluetoothctl_with_agent,
)


DEFAULT_PAIR_SCAN_SECONDS = 30
EXTRA_AUDIO_NAME_SCAN_SECONDS = 15
AUDIO_UUID_MARKERS = (
    "audio sink",
    "audio source",
    "a2dp",
    "headset",
    "headphones",
    "handsfree",
    "hands-free",
)
AUDIO_NAME_MARKERS = (
    "airpods",
    "buds",
    "earbud",
    "headphone",
    "headphones",
    "headset",
    "speaker",
    "sound",
)
DEVICE_LINE_PATTERN = re.compile(
    r"(?:\[[^\]]+\]\s+)?Device\s+([0-9A-Fa-f:]{17})\s+(.+)$"
)
DEVICE_NAME_CHANGE_PATTERN = re.compile(
    r"(?:\[[^\]]+\]\s+)?Device\s+([0-9A-Fa-f:]{17})\s+(Name|Alias):\s+(.+)$"
)
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
MAC_ADDRESS_PATTERN = re.compile(r"^[0-9A-Fa-f:]{17}$")
INFO_NAME_PATTERN = re.compile(r"^\s*(Alias|Name):\s+(.+)$")
DEVICE_PROPERTY_PREFIXES = (
    "advertising data",
    "appearance:",
    "class:",
    "connected:",
    "icon:",
    "legacy pairing:",
    "manufacturerdata",
    "modalias:",
    "paired:",
    "rssi:",
    "service data",
    "txpower:",
    "uuids:",
)


@dataclass(frozen=True)
class BluetoothDevice:
    address: str
    name: str


@dataclass(frozen=True)
class BluetoothActionResult:
    success: bool
    message: str
    enabled: bool | None = None
    connected: bool | None = None
    device_address: str = ""
    device_name: str = ""
    devices: tuple[BluetoothDevice, ...] = ()


BluetoothctlRunner = Callable[[Sequence[str], float], CommandResult]
BluetoothScanner = Callable[[float, BluetoothctlRunner], list[BluetoothDevice]]


def bluetooth_powered(runner: BluetoothctlRunner = run_bluetoothctl) -> bool:
    result = runner(["show"], 5.0)
    return "Powered: yes" in result.output


def wait_for_bluetooth_power(
    enabled: bool,
    runner: BluetoothctlRunner = run_bluetoothctl,
    timeout: float = 3.0,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if bluetooth_powered(runner) == enabled:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def set_bluetooth_power(
    enabled: bool,
    runner: BluetoothctlRunner = run_bluetoothctl,
) -> BluetoothActionResult:
    if enabled:
        unblock_bluetooth_adapter()

    result = runner(["power", "on" if enabled else "off"], 8.0)
    if result.returncode != 0:
        return BluetoothActionResult(
            False,
            short_message(result.output, fallback="Bluetooth unavailable"),
            enabled=None,
        )

    changed = wait_for_bluetooth_power(enabled, runner)
    powered = enabled if changed else bluetooth_powered(runner)
    if powered != enabled:
        return BluetoothActionResult(
            False,
            "Bluetooth did not change",
            enabled=powered,
            connected=False if not powered else None,
        )

    return BluetoothActionResult(
        True,
        "Bluetooth enabled" if enabled else "Bluetooth disabled",
        enabled=enabled,
        connected=False if not enabled else None,
    )


def unblock_bluetooth_adapter() -> None:
    rfkill = shutil.which("rfkill")
    if not rfkill:
        return
    try:
        subprocess.run(
            [rfkill, "unblock", "bluetooth"],
            capture_output=True,
            check=False,
            text=True,
            timeout=3.0,
        )
    except Exception:
        return


def pair_bluetooth_audio_device(
    scan_seconds: float = DEFAULT_PAIR_SCAN_SECONDS,
    runner: BluetoothctlRunner = run_bluetoothctl,
    scanner: BluetoothScanner | None = None,
) -> BluetoothActionResult:
    scan_result = scan_bluetooth_devices(scan_seconds, runner, scanner)
    if not scan_result.success or not scan_result.devices:
        return scan_result
    return pair_bluetooth_device(scan_result.devices[0], runner)


def scan_bluetooth_devices(
    scan_seconds: float = DEFAULT_PAIR_SCAN_SECONDS,
    runner: BluetoothctlRunner = run_bluetoothctl,
    scanner: BluetoothScanner | None = None,
) -> BluetoothActionResult:
    power_result = set_bluetooth_power(True, runner)
    if not power_result.success:
        return power_result

    runner(["agent", "NoInputNoOutput"], 5.0)
    runner(["default-agent"], 5.0)
    runner(["pairable", "on"], 5.0)

    scan = scanner or scan_for_devices
    devices = named_devices(enrich_device_names(scan(scan_seconds, runner), runner))
    audio_devices = audio_candidates(devices, runner)
    if audio_devices and not preferred_named_audio_devices(audio_devices, runner):
        devices = named_devices(
            enrich_device_names(
                merged_devices(devices, scan(EXTRA_AUDIO_NAME_SCAN_SECONDS, runner)),
                runner,
            )
        )
        audio_devices = audio_candidates(devices, runner)
    if audio_devices:
        devices = sorted(audio_devices, key=lambda device: audio_device_sort_key(device, runner))
    runner(["scan", "off"], 3.0)
    if not devices:
        return BluetoothActionResult(False, "No devices found", enabled=True)

    return BluetoothActionResult(
        True,
        "Choose a device",
        enabled=True,
        devices=tuple(devices),
    )


def pair_bluetooth_device(
    device: BluetoothDevice,
    runner: BluetoothctlRunner = run_bluetoothctl,
) -> BluetoothActionResult:
    # Keep the synchronous entry point on the same verified path as the OLED.
    from .bluetooth_connection import connect_speaker, playback_endpoint_ready
    from .bluetooth_speakers import Speaker, SpeakerError

    device = enrich_device_name(device, runner)
    try:
        connect_speaker(
            Speaker(device.address, device.name), lambda _stage: None, runner=runner,
            pair=run_bluetoothctl_with_agent if runner is run_bluetoothctl else runner,
            ready=playback_endpoint_ready,
        )
    except SpeakerError as error:
        return BluetoothActionResult(False, f"{error.stage} failed", enabled=True)
    return BluetoothActionResult(True, "Speaker ready", enabled=True, connected=True,
                                 device_address=device.address, device_name=device.name)


def unpair_connected_bluetooth_audio_device(
    runner: BluetoothctlRunner = run_bluetoothctl,
) -> BluetoothActionResult:
    device = connected_bluetooth_audio_device(runner) or paired_bluetooth_audio_device(runner)
    if device is None:
        return BluetoothActionResult(
            True,
            "Bluetooth unpaired",
            enabled=bluetooth_powered(runner),
            connected=False,
        )

    runner(["untrust", device.address], 5.0)
    runner(["unblock", device.address], 5.0)
    disconnect_result = runner(["disconnect", device.address], 10.0)
    print(
        f"Bluetooth unpair {device.address} {device.name}: "
        f"disconnect={disconnect_result.returncode} {disconnect_result.output}"
    )
    if disconnect_result.returncode == 0 or "Successful disconnected" in disconnect_result.output:
        return BluetoothActionResult(
            True,
            "Bluetooth unpaired",
            enabled=True,
            connected=False,
            device_address=device.address,
            device_name=device.name,
        )

    return BluetoothActionResult(
        False,
        short_message(disconnect_result.output, fallback="Could not unpair"),
        enabled=True,
        connected=True,
        device_address=device.address,
        device_name=device.name,
    )


def scan_for_devices(
    scan_seconds: float,
    runner: BluetoothctlRunner = run_bluetoothctl,
) -> list[BluetoothDevice]:
    scan_output = run_timed_scan(scan_seconds)
    return named_devices(
        enrich_device_names(
            merged_devices(
                parse_bluetooth_devices(scan_output),
                parse_bluetooth_devices(runner(["devices"], 5.0).output),
            ),
            runner,
        )
    )


def run_timed_scan(scan_seconds: float) -> str:
    timeout_seconds = max(1, int(scan_seconds))
    try:
        completed = subprocess.run(
            ["bluetoothctl", "--timeout", str(timeout_seconds), "scan", "on"],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds + 5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return combine_output(completed.stdout, completed.stderr)


def parse_bluetooth_devices(output: str) -> list[BluetoothDevice]:
    devices: dict[str, BluetoothDevice] = {}
    for line in output.splitlines():
        line = strip_ansi(line).strip()
        name_match = DEVICE_NAME_CHANGE_PATTERN.search(line)
        if name_match:
            address = name_match.group(1).upper()
            name = name_match.group(3).strip()
            if name and not is_mac_address(name.replace("-", ":")):
                devices[address] = BluetoothDevice(address, name)
            continue

        match = DEVICE_LINE_PATTERN.search(line)
        if not match:
            continue
        address = match.group(1).upper()
        name = name_without_mac_fallback(match.group(2).strip())
        if is_device_property_update(name):
            continue
        devices.setdefault(address, BluetoothDevice(address, name or address))
    return list(devices.values())


def strip_ansi(value: str) -> str:
    return ANSI_PATTERN.sub("", value)


def name_without_mac_fallback(name: str) -> str:
    normalized = name.replace("-", ":")
    return "" if is_mac_address(normalized) else name


def is_device_property_update(value: str) -> bool:
    lower = value.strip().lower()
    return any(lower.startswith(prefix) for prefix in DEVICE_PROPERTY_PREFIXES)


def merged_devices(*groups: list[BluetoothDevice]) -> list[BluetoothDevice]:
    devices: dict[str, BluetoothDevice] = {}
    for group in groups:
        for device in group:
            devices.setdefault(device.address, device)
    return list(devices.values())


def named_devices(devices: list[BluetoothDevice]) -> list[BluetoothDevice]:
    return [device for device in devices if has_display_name(device)]


def has_display_name(device: BluetoothDevice) -> bool:
    if not device.name:
        return False
    if device.name == device.address:
        return False
    return not is_mac_address(device.name.replace("-", ":"))


def enrich_device_names(
    devices: list[BluetoothDevice],
    runner: BluetoothctlRunner = run_bluetoothctl,
) -> list[BluetoothDevice]:
    return [enrich_device_name(device, runner) for device in devices]


def enrich_device_name(
    device: BluetoothDevice,
    runner: BluetoothctlRunner = run_bluetoothctl,
) -> BluetoothDevice:
    if device.name and not is_mac_address(device.name):
        return device
    info = runner(["info", device.address], 5.0).output
    name = name_from_info(info)
    if not name:
        return device
    return BluetoothDevice(device.address, name)


def name_from_info(info: str) -> str:
    names: dict[str, str] = {}
    for line in info.splitlines():
        match = INFO_NAME_PATTERN.match(line)
        if match:
            names[match.group(1)] = match.group(2).strip()
    return names.get("Alias") or names.get("Name") or ""


def is_mac_address(value: str) -> bool:
    return bool(MAC_ADDRESS_PATTERN.match(value.strip()))


def audio_candidates(
    devices: list[BluetoothDevice],
    runner: BluetoothctlRunner = run_bluetoothctl,
) -> list[BluetoothDevice]:
    candidates = []
    for device in devices:
        info = runner(["info", device.address], 5.0).output
        name = name_from_info(info) or device.name
        if audio_device_info(info) or audio_device_name(name):
            candidates.append(device)
    return candidates


def preferred_named_audio_devices(
    devices: list[BluetoothDevice],
    runner: BluetoothctlRunner = run_bluetoothctl,
) -> list[BluetoothDevice]:
    preferred = []
    for device in devices:
        info = runner(["info", device.address], 5.0).output
        name = name_from_info(info) or device.name
        if audio_device_name(name):
            preferred.append(device)
    return preferred


def audio_device_sort_key(
    device: BluetoothDevice,
    runner: BluetoothctlRunner = run_bluetoothctl,
) -> tuple[int, str]:
    info = runner(["info", device.address], 5.0).output
    name = (name_from_info(info) or device.name).lower()
    if audio_device_name(name):
        return (0, name)
    if audio_device_info(info):
        return (1, name)
    return (2, name)


def audio_device_info(info: str) -> bool:
    lower = info.lower()
    return any(marker in lower for marker in AUDIO_UUID_MARKERS)


def audio_device_name(name: str) -> bool:
    lower = name.lower()
    return any(marker in lower for marker in AUDIO_NAME_MARKERS)


def connected_info(info: str) -> bool:
    return "Connected: yes" in info


def connection_successful(output: str) -> bool:
    lower = output.lower()
    return "connection successful" in lower or "connected: yes" in lower


def connected_bluetooth_audio_device(
    runner: BluetoothctlRunner = run_bluetoothctl,
) -> BluetoothDevice | None:
    devices = parse_bluetooth_devices(runner(["devices", "Connected"], 5.0).output)
    if not devices:
        devices = parse_bluetooth_devices(runner(["devices"], 5.0).output)
    for device in devices:
        info = runner(["info", device.address], 5.0).output
        name = name_from_info(info) or device.name
        if connected_info(info) and (audio_device_info(info) or audio_device_name(name)):
            return enrich_device_name(device, runner)
    return None


def paired_bluetooth_audio_device(
    runner: BluetoothctlRunner = run_bluetoothctl,
) -> BluetoothDevice | None:
    devices = parse_bluetooth_devices(runner(["devices"], 5.0).output)
    for device in devices:
        info = runner(["info", device.address], 5.0).output
        name = name_from_info(info) or device.name
        lower = info.lower()
        remembered = "paired: yes" in lower or "trusted: yes" in lower or "bonded: yes" in lower
        if remembered and (audio_device_info(info) or audio_device_name(name)):
            return enrich_device_name(device, runner)
    return None


def bluealsa_available() -> bool:
    if shutil.which("bluealsa") or shutil.which("bluealsa-aplay"):
        return True
    try:
        completed = subprocess.run(
            ["aplay", "-L"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return "bluealsa" in completed.stdout.lower()


def preferred_bluetooth_alsa_device(address: str = "") -> str | None:
    if address:
        if not connected_info(run_bluetoothctl(["info", address]).output):
            return None
        return f"bluealsa:DEV={address},PROFILE=a2dp"
    if not bluealsa_available():
        return None
    device = connected_bluetooth_audio_device()
    if device is None:
        return None
    return f"bluealsa:DEV={device.address},PROFILE=a2dp"


def short_message(output: str, fallback: str) -> str:
    for line in output.splitlines():
        line = line.strip()
        if line:
            return line[:32]
    return fallback
