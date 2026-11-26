"""Choose the frame shown on the OLED and retain its scroll, overlay and wake state."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .bedtime import BedtimeTimer
from .config import AppConfig
from .dial_timing import DialTiming
from .display_frames import (
    SCREEN_OFF,
    DisplayFrame,
    HomeFrame,
    ScreenSaverFrame,
    SplashFrame,
    TwoLineFrame,
    VolumeFrame,
    WifiSetupFrame,
)
from .listening_timer import ListeningSleepTimer
from .network import setup_address_frame
from .oled_catalogue import OledCatalogue
from .oled_display import Sh1122Display, screen_saver_contrast_percent
from .oled_input import OledInputQueue, consume_screen_saver_wake_input
from .rotary_ui import ApplianceMenu, Screen
from .screen_power import screen_off_due
from .screen_saver import screen_saver_frame, screen_saver_is_active, screen_saver_words_for_mode
from .wifi import SETUP_HOTSPOT_SSID, SETUP_HOTSPOT_URL, WifiStatus


DEFAULT_TITLE_SCROLL_DELAY_SECONDS = 2.0
DEFAULT_TITLE_SCROLL_PX_PER_SECOND = 16.0
DEFAULT_TITLE_SCROLL_LOOP_PAUSE_SECONDS = 2.0
DEFAULT_VOLUME_OVERLAY_SECONDS = 3.0
DEFAULT_IP_OVERLAY_SECONDS = 5.0
DEFAULT_ACTIVE_BRIGHTNESS_PERCENT = 100
BOOK_LOADING_FRAME = TwoLineFrame("Opening book", "Press to cancel")


def frame_for_wifi_status(status: WifiStatus, lan_ip: str | None = None) -> DisplayFrame:
    if status.connected and status.ssid == SETUP_HOTSPOT_SSID:
        return setup_frame()

    if status.connected:
        return setup_address_frame(lan_ip)

    return setup_frame()


def splash_frame() -> SplashFrame:
    return SplashFrame("chapter")


def setup_frame() -> WifiSetupFrame:
    return WifiSetupFrame(SETUP_HOTSPOT_SSID, SETUP_HOTSPOT_URL)


def frame_for_boot_state(
    status: WifiStatus | None,
    home_frame: DisplayFrame | None = None,
    lan_ip: str | None = None,
    startup_waiting: bool = False,
) -> DisplayFrame:
    if home_frame is not None:
        return home_frame
    if status is None or (startup_waiting and not status.connected):
        return splash_frame()
    if not status.connected or status.ssid == SETUP_HOTSPOT_SSID:
        return setup_frame()
    return setup_address_frame(lan_ip)


def scrolled_home_frame(
    frame: DisplayFrame,
    elapsed_seconds: float,
    delay_seconds: float = DEFAULT_TITLE_SCROLL_DELAY_SECONDS,
    pixels_per_second: float = DEFAULT_TITLE_SCROLL_PX_PER_SECOND,
    loop_pause_seconds: float = DEFAULT_TITLE_SCROLL_LOOP_PAUSE_SECONDS,
) -> DisplayFrame:
    # The display applies the pause when wrapping text. Keep the old keyword for callers.
    if not isinstance(frame, HomeFrame) or elapsed_seconds < delay_seconds:
        return frame
    active_seconds = elapsed_seconds - delay_seconds
    return replace(
        frame,
        title_scroll_px=round(active_seconds * pixels_per_second),
        title_scrolling=True,
    )


def refresh_home_cache(
    cached_home_frame: DisplayFrame | None,
    next_home_frame: DisplayFrame,
    home_frame_loaded_at: float,
    now: float,
) -> tuple[DisplayFrame, float]:
    if same_home_cache_subject(cached_home_frame, next_home_frame):
        return next_home_frame, home_frame_loaded_at
    return next_home_frame, now


def same_home_cache_subject(frame: DisplayFrame | None, next_frame: DisplayFrame) -> bool:
    if isinstance(frame, HomeFrame) and isinstance(next_frame, HomeFrame):
        return (
            frame.title == next_frame.title
            and frame.author == next_frame.author
            and frame.series_position == next_frame.series_position
            and frame.is_podcast == next_frame.is_podcast
        )
    return frame == next_frame


def frame_with_mute_indicator(frame: DisplayFrame, muted: bool) -> DisplayFrame:
    if not muted or isinstance(frame, VolumeFrame):
        return frame
    if hasattr(frame, "muted"):
        return replace(frame, muted=True)
    return frame


def frame_with_status_indicators(
    frame: DisplayFrame,
    muted: bool,
    bluetooth_connected: bool,
) -> DisplayFrame:
    if hasattr(frame, "bluetooth_connected"):
        frame = replace(frame, bluetooth_connected=bluetooth_connected)
    return frame_with_mute_indicator(frame, muted)


def update_display_brightness(display, percent: int, current_percent: int) -> int:
    percent = min(max(int(percent), 0), 100)
    if percent == current_percent:
        return current_percent
    try:
        display.set_brightness(percent)
    except Exception as error:
        print(f"OLED brightness update failed: {error}")
        return current_percent
    return percent


class OledPresentation:
    def __init__(
        self,
        display: Sh1122Display,
        started_at: float,
        idle_seconds: float,
        frames_per_second: float,
    ) -> None:
        self.display = display
        self.timing = DialTiming()
        self.started_at = started_at
        self.idle_seconds = idle_seconds
        self.frames_per_second = frames_per_second
        self.panel_off = False
        self.saver_visible = False
        self.last_input_at = started_at
        self.wake_quiet_until = 0.0
        self.brightness = DEFAULT_ACTIVE_BRIGHTNESS_PERCENT
        self.home_frame: DisplayFrame | None = None
        self.home_loaded_at = 0.0
        self.overlay: DisplayFrame | None = None
        self.overlay_until = 0.0
        self.opening_key = ""

    def show(self, frame: DisplayFrame) -> None:
        self.timing.show(SCREEN_OFF if self.panel_off else frame, self.display.show)

    def set_brightness(self, percent: int) -> None:
        self.brightness = update_display_brightness(self.display, percent, self.brightness)

    def update_panel_power(
        self, config: AppConfig, events: OledInputQueue, now: float, local_time: datetime
    ) -> bool:
        due = screen_off_due(config, now, local_time, self.started_at, events.last_dial_at)
        woke = self.panel_off and events.consume_panel_wake()
        # A gesture already queued while the screen was lit still belongs to the menu.
        self.panel_off = due and not woke and (self.panel_off or events.empty())
        return woke

    def consume_saver_wake(self, events: OledInputQueue, now: float, woke_panel: bool) -> bool:
        if self.panel_off or woke_panel:
            self.wake_quiet_until = 0.0
            return False
        woke, self.wake_quiet_until = consume_screen_saver_wake_input(
            events, self.saver_visible, now, self.wake_quiet_until
        )
        return woke

    def invalidate_home(self, now: float) -> None:
        self.home_frame = None
        self.home_loaded_at = now

    def update_overlay(self, frame: DisplayFrame | None, now: float, *, loading: bool) -> None:
        if frame is not None:
            self.overlay = frame
            if frame == BOOK_LOADING_FRAME:
                duration = 60.0
            elif isinstance(frame, TwoLineFrame):
                duration = DEFAULT_IP_OVERLAY_SECONDS
            else:
                duration = DEFAULT_VOLUME_OVERLAY_SECONDS
            self.overlay_until = now + duration
        if loading and self.overlay is None:
            self.overlay = BOOK_LOADING_FRAME
            self.overlay_until = now + 60.0

    def draw_overlay(
        self, now: float, brightness: int, muted: bool, bluetooth_connected: bool
    ) -> bool:
        if self.overlay is None or now >= self.overlay_until:
            self.overlay = None
            self.opening_key = ""
            return False
        self.set_brightness(brightness)
        self.saver_visible = False
        self.show(frame_with_status_indicators(self.overlay, muted, bluetooth_connected))
        return True

    def menu_frame(
        self,
        menu: ApplianceMenu,
        catalogue: OledCatalogue,
        config: AppConfig,
        status: WifiStatus | None,
        now: float,
        startup_waiting: bool,
    ) -> DisplayFrame:
        if status is None or not status.connected or status.ssid == SETUP_HOTSPOT_SSID:
            catalogue.refresh_when_online()
            self.home_frame = None
            offline_menu = menu.books or menu.screen in (Screen.BLUETOOTH, Screen.BLUETOOTH_DEVICES)
            return frame_for_boot_state(
                status, menu.render() if offline_menu else None, startup_waiting=startup_waiting
            )

        loading_config = catalogue.start_if_due(now)
        if loading_config is not None:
            menu.set_library_sort_mode(loading_config.library_sort_mode)
        frame = menu.render()
        if menu.screen == Screen.LIBRARY and not menu.books:
            if not config.is_ready:
                frame = setup_address_frame()
            elif catalogue.in_progress:
                frame = TwoLineFrame("Loading books", "Connecting...")
        self.home_frame, self.home_loaded_at = refresh_home_cache(
            self.home_frame, frame, self.home_loaded_at, now
        )
        return scrolled_home_frame(self.home_frame, now - self.home_loaded_at)

    def draw_menu(
        self,
        frame: DisplayFrame,
        menu: ApplianceMenu,
        config: AppConfig,
        now: float,
        bedtime: BedtimeTimer,
        sleep_timer: ListeningSleepTimer,
        brightness: int,
        muted: bool,
        bluetooth_connected: bool,
    ) -> None:
        idle = (
            screen_saver_is_active(now, self.last_input_at, self.idle_seconds)
            and not sleep_timer.prompt_active
        )
        show_saver = not self.panel_off and (bedtime.sleeping or sleep_timer.sleeping or idle)
        if show_saver:
            mode = config.screen_saver_mode
            frame = screen_saver_frame(
                now,
                bedtime.sleep_started_at or sleep_timer.sleep_started_at or self.last_input_at,
                self.frames_per_second,
                mode=mode,
                words=screen_saver_words_for_mode(mode, menu.books),
                brightness_percent=config.screen_saver_dim_percent,
                clock_format=config.screen_saver_clock_format,
            )
        if isinstance(frame, ScreenSaverFrame):
            brightness = min(screen_saver_contrast_percent(frame.brightness_percent), brightness)
        self.set_brightness(brightness)
        self.saver_visible = show_saver
        self.show(frame_with_status_indicators(frame, muted, bluetooth_connected))
