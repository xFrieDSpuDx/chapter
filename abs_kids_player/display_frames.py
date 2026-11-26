"""Describe the content of each screen independently of the menu and display driver."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


BLUETOOTH_DEVICE_NAME_MAX_CHARS = 20
MORE_ACTION_LABEL = "..."


@dataclass(frozen=True)
class ScreenOffFrame:
    top = ""
    bottom = ""


SCREEN_OFF = ScreenOffFrame()

@dataclass(frozen=True)
class TwoLineFrame:
    top: str
    bottom: str
    muted: bool = False
    bluetooth_connected: bool = False


@dataclass(frozen=True)
class HomeFrame:
    title: str
    author: str
    series_position: str = ""
    title_scroll_px: int = 0
    title_scrolling: bool = False
    section_letter: str = ""
    section_letter_fill: int = 0
    muted: bool = False
    bluetooth_connected: bool = False
    is_podcast: bool = False

    @property
    def top(self) -> str:
        return self.title

    @property
    def bottom(self) -> str:
        return author_line(self.author)


@dataclass(frozen=True)
class SplashFrame:
    text: str

    @property
    def top(self) -> str:
        return self.text

    @property
    def bottom(self) -> str:
        return ""


@dataclass(frozen=True)
class ScreenSaverFrame:
    frame_index: int
    mode: str = "chapter"
    words: tuple[str, ...] = ("chapter",)
    clock_text: str = ""
    brightness_percent: int = 15

    @property
    def top(self) -> str:
        return "Screen saver"

    @property
    def bottom(self) -> str:
        return ""


@dataclass(frozen=True)
class VolumeFrame:
    percent: int
    muted: bool = False
    bluetooth_connected: bool = False

    @property
    def top(self) -> str:
        return "Muted" if self.is_muted else "Volume"

    @property
    def bottom(self) -> str:
        return volume_bar(self.percent)

    @property
    def is_muted(self) -> bool:
        return self.muted or self.percent <= 0


@dataclass(frozen=True)
class PlayingFrame:
    chapter_text: str
    remaining_text: str
    actions: tuple[str, ...]
    selected_index: int
    title_scroll_px: int = 0
    muted: bool = False
    bluetooth_connected: bool = False

    @property
    def top(self) -> str:
        return f"{self.chapter_text} {self.remaining_text}".strip()

    @property
    def bottom(self) -> str:
        return _highlight_option(self.actions, self.selected_index)


@dataclass(frozen=True)
class ChapterConfirmFrame:
    direction: str
    selected_index: int = 0
    options: tuple[str, str] = ("Yes", "No")
    muted: bool = False
    bluetooth_connected: bool = False

    @property
    def top(self) -> str:
        return f"Are you sure you want to listen to the {self.direction} chapter?"

    @property
    def bottom(self) -> str:
        return _highlight_option(self.options, self.selected_index)


@dataclass(frozen=True)
class SleepConfirmFrame:
    countdown_seconds: int
    options: tuple[str, ...] = ("Yes",)
    selected_index: int = 0
    muted: bool = False
    bluetooth_connected: bool = False

    @property
    def top(self) -> str:
        return f"Are you still listening? {max(0, self.countdown_seconds)}"

    @property
    def bottom(self) -> str:
        return "[Yes]"


@dataclass(frozen=True)
class ResumeChoiceFrame:
    title: str
    selected_index: int = 0
    options: tuple[str, ...] = ("Continue", "Start over")
    muted: bool = False
    bluetooth_connected: bool = False

    @property
    def top(self) -> str:
        return self.title

    @property
    def bottom(self) -> str:
        return _highlight_option(self.options, self.selected_index)


@dataclass(frozen=True)
class StartupResumeFrame:
    title: str
    selected_index: int = 0
    options: tuple[str, str] = ("Continue", "Home")
    muted: bool = False
    bluetooth_connected: bool = False

    @property
    def top(self) -> str:
        return f"You were listening to {self.title}"

    @property
    def bottom(self) -> str:
        return _highlight_option(self.options, self.selected_index)


@dataclass(frozen=True)
class BluetoothMenuFrame:
    enabled: bool
    selected_index: int = 2
    options: tuple[str, str, str] = ("Back", "Disable", "Speakers")
    muted: bool = False
    bluetooth_connected: bool = False

    @property
    def top(self) -> str:
        return "Bluetooth On" if self.enabled else "Bluetooth Off"

    @property
    def bottom(self) -> str:
        return _highlight_option(self.options, self.selected_index)


@dataclass(frozen=True)
class BluetoothDevicesFrame:
    device_name: str
    selected_index: int
    total_count: int
    muted: bool = False
    bluetooth_connected: bool = False
    heading: str = "Bluetooth Devices"

    @property
    def top(self) -> str:
        return self.heading

    @property
    def bottom(self) -> str:
        suffix = f" {self.selected_index + 1}/{self.total_count}" if self.total_count else ""
        return f"{shorten_device_name(self.device_name)}{suffix}".strip()


@dataclass(frozen=True)
class WifiSetupFrame:
    ssid: str
    url: str
    muted: bool = False
    bluetooth_connected: bool = False

    @property
    def top(self) -> str:
        return f"Connect to {self.ssid}"

    @property
    def bottom(self) -> str:
        return f"Browse to {self.url}"


DisplayFrame = Union[
    ScreenOffFrame,
    TwoLineFrame,
    HomeFrame,
    SplashFrame,
    ScreenSaverFrame,
    VolumeFrame,
    PlayingFrame,
    ChapterConfirmFrame,
    SleepConfirmFrame,
    ResumeChoiceFrame,
    StartupResumeFrame,
    BluetoothMenuFrame,
    BluetoothDevicesFrame,
    WifiSetupFrame,
]


def volume_bar(percent: int, width: int = 12) -> str:
    percent = min(max(percent, 0), 100)
    filled = round((percent / 100) * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def author_line(author: str) -> str:
    author = author.strip()
    return f"By {author}" if author else ""


def shorten_device_name(name: str, max_chars: int = BLUETOOTH_DEVICE_NAME_MAX_CHARS) -> str:
    clean = " ".join(name.split())
    if len(clean) <= max_chars:
        return clean
    return clean[: max(0, max_chars - 3)].rstrip() + "..."


def _highlight_option(options: tuple[str, ...], selected_index: int) -> str:
    parts = list(options)
    parts[selected_index] = f"[{parts[selected_index]}]"
    return " ".join(parts)
