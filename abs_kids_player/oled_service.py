from __future__ import annotations

import argparse
import faulthandler
import queue
import signal
import threading
import time
import traceback
from concurrent.futures import Future
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from .api import AudiobookshelfClient, AudiobookshelfError
from .appliance_io import (
    AmpShutdownPin,
    Ky040Pins,
    NavigationRotaryInput,
    VolumeRotaryInput,
)
from .audio import DEFAULT_MIXER_CONTROLS, VolumeState, play_boot_chime, play_control_click
from .audio_output_test import AUDIO_TEST_COMMAND, AudioOutputTest
from .bedtime import BedtimeTimer, night_mode_active
from .bluetooth_audio import (
    BluetoothActionResult,
    bluetooth_powered,
    connected_bluetooth_audio_device,
    preferred_bluetooth_alsa_device,
    set_bluetooth_power,
)
from .build_info import log_installed_build
from .catalogue_settings import (
    CATALOGUE_COMMAND,
    apply_library_choice,
    catalogue_request_key,
    catalogue_status_key,
)
from .config import (
    AppConfig,
    ConfigReadError,
    load_config,
)
from .device_status import DeviceStatusCache
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
from .download_store import books_with_downloads
from .gstreamer_runtime import GStreamerRuntime
from .library import load_library_books, sorted_books_for_menu
from .library_cache import load_cached_books
from .listening_control import (
    SESSION_ACTIONS,
    apply_listening_control,
    end_book_selection,
    finish_menu_book,
    record_control_outcome,
    requires_active_session,
    seek_listening_position,
    session_matches,
)
from .listening_queue import next_queued_book, read_queue, remove_started_book
from .listening_status import ListeningDetailsPublisher
from .listening_timer import ListeningSleepTimer, SleepTimerEvent
from .models import Book, PlaybackSession
from .network import setup_address_frame
from .oled_bookmarks import OledBookmarkPicker
from .oled_catalogue import OledCatalogue
from .oled_display import Sh1122Display, screen_saver_contrast_percent
from .oled_input import (
    OledInputQueue,
    consume_screen_saver_wake_input,
    drain_input_events,
    handle_oled_input_events,
    input_activity_seen,
)
from .oled_shelf import OledShelf
from .oled_web_status import (
    WebStatusPublisher,
)
from .oled_web_status import (
    web_status_for_playback as web_status_for_playback,
)
from .oled_web_status import (
    web_status_for_playback_state as web_status_for_playback_state,
)
from .playback import GStreamerPlayback, PlaybackError
from .playback_owner import PlaybackOwner, PlaybackSnapshot
from .playback_startup_history import record_startup_sample
from .player_control import (
    WebCommandInbox,
    WebPlayerCommand,
    WebPlayerStatus,
    consume_web_player_commands,
    load_web_player_status,
    save_command_result,
    save_web_player_status,
)
from .player_state import (
    OutputVolumes,
    clear_last_playback,
    load_bluetooth_volume,
    load_player_state,
    load_saved_volume,
    save_last_playback,
    save_output_volumes,
)
from .podcast_catalogue import request_podcast_catalogue
from .progress_sync import ProgressSync, progress_account
from .rotary_ui import (
    ApplianceMenu,
    CommandType,
    MenuCommand,
    Screen,
    SpokenSelection,
    book_is_podcast,
)
from .screen_power import screen_off_due
from .screen_saver import (
    DEFAULT_SCREEN_SAVER_FRAMES_PER_SECOND,
    DEFAULT_SCREEN_SAVER_IDLE_SECONDS,
    screen_saver_frame,
    screen_saver_is_active,
    screen_saver_words_for_mode,
)
from .software_update import load_update_state, mark_update_display_ready, update_state_is_active
from .spoken_navigation import SpokenNavigationFeedback
from .volume_controls import (
    DEFAULT_UNMUTE_VOLUME_PERCENT,
    effective_muted,
    restored_audible_volume_state,
    unmuted_volume,
)
from .wifi import SETUP_HOTSPOT_SSID, SETUP_HOTSPOT_URL, WifiStatus, wifi_status


DEFAULT_REFRESH_SECONDS = 0.14
DEFAULT_STATUS_REFRESH_SECONDS = 2.0
DEFAULT_SPLASH_SECONDS = 0.5
DEFAULT_STARTUP_NETWORK_GRACE_SECONDS = 30.0
DEFAULT_HOME_REFRESH_SECONDS = 60.0
DEFAULT_TITLE_SCROLL_DELAY_SECONDS = 2.0
DEFAULT_TITLE_SCROLL_PX_PER_SECOND = 16.0
DEFAULT_TITLE_SCROLL_LOOP_PAUSE_SECONDS = 2.0
DEFAULT_VOLUME_OVERLAY_SECONDS = 3.0
DEFAULT_IP_OVERLAY_SECONDS = 5.0
DEFAULT_BOOT_SPLASH_HANDOFF_PATH = "/tmp/audiobookshelf-player-oled-starting"
DEFAULT_BOOT_SPLASH_HANDOFF_SETTLE_SECONDS = 0.3
DEFAULT_PLAYBACK_STATE_SAVE_SECONDS = 5.0
DEFAULT_CONTROL_CLICK_MIN_INTERVAL_SECONDS = 0.045
CONTROL_CLICK_OUTPUT_HOLD_SECONDS = 0.15
DEFAULT_ACTIVE_BRIGHTNESS_PERCENT = 100
DEFAULT_RUNTIME_CONFIG_REFRESH_SECONDS = 1.0
DEFAULT_SOFTWARE_UPDATE_REFRESH_SECONDS = 0.2
BOOK_LOADING_FRAME = TwoLineFrame("Opening book", "Press to cancel")
BLUETOOTH_POWER_FRAME = TwoLineFrame("Bluetooth", "Updating...")
SOFTWARE_UPDATE_FRAME = TwoLineFrame("UPDATING", "DO NOT POWER OFF")


@dataclass(frozen=True)
class BluetoothStatusResult:
    enabled: bool
    connected: bool


def bluetooth_result_frame(result: BluetoothActionResult) -> DisplayFrame:
    if result.success and result.connected is False:
        return TwoLineFrame("Disconnected" if result.device_name else "Bluetooth off", "Internal speaker")
    if result.success and result.device_name:
        return TwoLineFrame("Speaker ready", result.device_name)
    if result.success:
        return TwoLineFrame("Bluetooth", "On" if result.enabled else "Off")
    return TwoLineFrame("Bluetooth failed", result.message)


class Shutdown:
    requested = False

    def request(self, _signum: int, _frame: object) -> None:
        self.requested = True


class ControlClickFeedback:
    def __init__(self, min_interval_seconds: float = DEFAULT_CONTROL_CLICK_MIN_INTERVAL_SECONDS) -> None:
        self.min_interval_seconds = min_interval_seconds
        self.last_click_at = float("-inf")

    def click(self, volume_percent: int, enable_output: Callable[[], None]) -> None:
        if volume_percent <= 0:
            return
        now = time.monotonic()
        if now - self.last_click_at < self.min_interval_seconds:
            return
        self.last_click_at = now
        enable_output()
        play_control_click(volume_percent)

    @property
    def needs_audio_output(self) -> bool:
        return time.monotonic() - self.last_click_at < CONTROL_CLICK_OUTPUT_HOLD_SECONDS


def enable_stack_dump_signal() -> None:
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True)
        print("Stack dump enabled on SIGUSR1")
    except Exception as error:
        print(f"Stack dump signal could not be enabled: {error}")


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


def load_home_frame(
    config: AppConfig | None = None,
    client_factory=AudiobookshelfClient,
    lan_ip: str | None = None,
) -> DisplayFrame:
    if config is None:
        config = load_config()
    if not config.is_ready:
        return setup_address_frame(lan_ip)

    try:
        return home_frame_for_books(load_library_books(config, client_factory))
    except AudiobookshelfError:
        return TwoLineFrame("Books unavailable", "Check setup")


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


def home_frame_for_books(books: list[Book]) -> DisplayFrame:
    if not books:
        return TwoLineFrame("No books", "Check library")
    book = books[0]
    return HomeFrame(
        book.display_title,
        book.author,
        series_position=book.series_position,
        is_podcast=book_is_podcast(book),
    )


def scrolled_home_frame(
    frame: DisplayFrame,
    elapsed_seconds: float,
    delay_seconds: float = DEFAULT_TITLE_SCROLL_DELAY_SECONDS,
    pixels_per_second: float = DEFAULT_TITLE_SCROLL_PX_PER_SECOND,
    loop_pause_seconds: float = DEFAULT_TITLE_SCROLL_LOOP_PAUSE_SECONDS,
) -> DisplayFrame:
    if not isinstance(frame, HomeFrame) or elapsed_seconds < delay_seconds:
        return frame
    active_seconds = elapsed_seconds - delay_seconds
    loop_pause_px = round(loop_pause_seconds * pixels_per_second)
    raw_scroll_px = round(active_seconds * pixels_per_second)
    if loop_pause_px and raw_scroll_px % loop_pause_px == 0:
        scroll_px = raw_scroll_px
    else:
        scroll_px = raw_scroll_px
    return HomeFrame(
        title=frame.title,
        author=frame.author,
        series_position=frame.series_position,
        title_scroll_px=scroll_px,
        title_scrolling=True,
        section_letter=frame.section_letter,
        section_letter_fill=frame.section_letter_fill,
        muted=frame.muted,
        bluetooth_connected=frame.bluetooth_connected,
        is_podcast=frame.is_podcast,
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


def set_amp_shutdown(
    amp_shutdown, muted: bool, volume_percent: int, *, feedback_active: bool = False
) -> bool:
    shutdown = effective_muted(volume_percent, muted) and not feedback_active
    if amp_shutdown is not None:
        amp_shutdown.set_shutdown(shutdown)
    return shutdown


def unmute_for_audible_playback_commands(
    commands: list[MenuCommand],
    fallback_percent: int,
    fallback_muted: bool,
    mixer_controls: tuple[str, ...] = DEFAULT_MIXER_CONTROLS,
    software_volume_only: bool = False,
) -> tuple[list[MenuCommand], int, bool, bool, bool]:
    if not effective_muted(fallback_percent, fallback_muted):
        return commands, fallback_percent, fallback_muted, False, False

    audible_command_types = {CommandType.PLAY_BOOK, CommandType.TOGGLE_PLAYBACK}
    if not any(command.type in audible_command_types for command in commands):
        return commands, fallback_percent, fallback_muted, False, False

    if software_volume_only:
        state = unmuted_volume(fallback_percent)
    else:
        state = restored_audible_volume_state(mixer_controls)

    resume_from_muted_play = any(command.type == CommandType.TOGGLE_PLAYBACK for command in commands)
    filtered_commands = [command for command in commands if command.type != CommandType.TOGGLE_PLAYBACK]
    return filtered_commands, state.percent, state.muted, True, resume_from_muted_play


def save_playback_state(playback: PlaybackOwner) -> bool:
    state = playback.state
    if state.session is None:
        return False
    try:
        save_last_playback(
            state.session.library_item_id, state.session.title, state.current_time,
            episode_id=state.session.episode_id, account=state.session.progress_account,
        )
    except OSError as error:
        print(f"Could not save playback position: {error}")
        return False
    return True


def arm_bedtime(
    timer: BedtimeTimer, minutes: int, now: float, menu: ApplianceMenu, rewind: bool
) -> TwoLineFrame:
    if not minutes:
        timer.cancel()
        return TwoLineFrame("Sleep timer", "Cancelled")
    if menu.session is None:
        return TwoLineFrame("Sleep timer", "Choose a book first")
    chapter_end = menu.current_chapter.end
    if minutes < 0 and chapter_end <= menu.current_time:
        return TwoLineFrame("Sleep timer", "Chapter end unavailable")
    session_id = menu.session.id or menu.session.library_item_id
    timer.arm(session_id, now, minutes, chapter_end, rewind)
    return TwoLineFrame("Sleep timer set", "End of chapter" if minutes < 0 else f"{minutes} minutes")


def poll_playback_safely(playback: PlaybackOwner) -> DisplayFrame | None:
    try:
        playback.poll()
    except PlaybackError as error:
        print(f"Playback poll failed: {error}")
        return TwoLineFrame("Playback stopped", "Check audio")
    return None


def pause_and_publish_playback(
    playback: PlaybackOwner,
    menu: ApplianceMenu,
    volume_percent: int,
    muted: bool,
    *, publisher: WebStatusPublisher | None = None,
) -> None:
    playback.pause(sync=True)
    menu.update_playback(playback.current_time, False)
    if publisher is None:
        publisher = WebStatusPublisher(save_web_player_status)
    publisher.publish(playback, volume_percent, muted)


def apply_sleep_timer_event(
    event: SleepTimerEvent,
    timer: ListeningSleepTimer,
    now: float,
    playback: PlaybackOwner,
    menu: ApplianceMenu,
    volume_percent: int,
    muted: bool,
    *, publisher: WebStatusPublisher | None = None,
) -> None:
    if event in ("prompt", "sleep"):
        pause_and_publish_playback(playback, menu, volume_percent, muted, publisher=publisher)
    if event == "prompt":
        menu.show_sleep_confirmation(timer.countdown_seconds(now))
    elif event == "sleep":
        menu.dismiss_sleep_confirmation()
    elif event == "cancel":
        menu.dismiss_sleep_confirmation()
        if playback.session is not None and not playback.is_playing:
            playback.resume(sync=False)
            menu.update_playback(playback.current_time, playback.is_playing)
    if timer.prompt_active:
        menu.update_sleep_confirmation(timer.countdown_seconds(now))


def handle_web_player_commands(
    playback: PlaybackOwner,
    menu: ApplianceMenu,
    volume_percent: int,
    muted: bool,
    *,
    commands: list[WebPlayerCommand] | None = None,
    request_catalogue: Callable[[WebPlayerCommand], None] | None = None,
    audio_test: AudioOutputTest | None = None,
    night_volume_limit: int = 100,
    request_book: Callable[[WebPlayerCommand], None] | None = None,
    listening_control: Callable[[WebPlayerCommand], bool | Future[bool]] | None = None,
) -> tuple[int, bool, bool, bool]:
    changed = False
    refresh_library = False
    if commands is None:
        commands = consume_web_player_commands()
    for index, command in enumerate(commands):
        try:
            if command.action in ("stop", "cancel_open"):
                matches = command.catalogue_key == catalogue_status_key(load_config())
                changed = True
                applied = matches and end_book_selection(playback, menu, command.session_key)
                save_command_result(command, "applied" if applied else "superseded")
                continue
            if requires_active_session(command) and not session_matches(playback, command):
                save_command_result(command, "superseded")
                continue
            if command.action == "volume" and index + 1 < len(commands):
                following = commands[index + 1]
                if following.action == "volume" and following.value is not None:
                    save_command_result(command, "superseded")
                    continue
            if command.action == "play_book" and request_book is not None:
                request_book(command)
                continue
            if command.action == "refresh_library":
                playback.cancel_book_request()
                playback.pause(sync=True)
                playback.stop()
                clear_last_playback()
                menu.set_books([])
                menu.reset_to_library()
                changed = True
                refresh_library = True
                continue
            if command.action == "reload_catalogue":
                refresh_library = True
                continue
            if command.action == CATALOGUE_COMMAND and request_catalogue is not None:
                request_catalogue(command)
                continue

            if command.action == AUDIO_TEST_COMMAND and audio_test is not None:
                audio_test.request(
                    command, playback_busy=playback.is_playing or playback.loading_book,
                    volume_percent=volume_percent, muted=muted, night_limit=night_volume_limit,
                )
                continue

            if command.action == "resort_library":
                sort_mode = load_config().library_sort_mode
                menu.set_library_sort_mode(sort_mode)
                menu.set_books(sorted_books_for_menu(menu.books, sort_mode))
                changed = True
                continue

            if command.action == "pause_book":
                session = playback.session
                if (
                    session is None or session.library_item_id != command.book_id or playback.loading_book
                    or command.catalogue_key != catalogue_request_key(load_config())
                ):
                    save_command_result(command, "rejected")
                    continue

            if command.action in ("pause", "pause_book"):
                had_request = getattr(playback, "loading_book", False)
                playback.cancel_book_request()
                applied = playback.pause(sync=True)
                if applied:
                    menu.update_playback(playback.current_time, playback.is_playing)
                    changed = True
                save_command_result(command, "applied" if applied or playback.session or had_request else "rejected")
                continue

            if command.action == "play":
                resumed_volume = volume_percent if volume_percent > 0 else DEFAULT_UNMUTE_VOLUME_PERCENT
                applied = playback.resume(sync=False, percent=resumed_volume, muted=False)
                if applied:
                    volume_percent = resumed_volume
                    muted = False
                    menu.update_playback(playback.current_time, playback.is_playing)
                    changed = True
                save_command_result(command, "applied" if applied or playback.is_playing else "rejected")
                continue

            if command.action == "volume" and command.value is not None:
                target = min(max(command.value, 0), 100)
                applied_volume = playback.set_volume_state(target, target == 0)
                volume_percent = target
                muted = volume_percent <= 0
                if volume_percent <= 0 and playback.pause(sync=False):
                    menu.update_playback(playback.current_time, playback.is_playing)
                if isinstance(applied_volume, Future):
                    applied_volume.add_done_callback(
                        lambda result, submitted=command: save_command_result(
                            submitted, "rejected" if result.cancelled() or result.exception() else "applied"
                        )
                    )
                else:
                    save_command_result(command, "applied")
                changed = True
                continue
            if command.action in ("mute", "unmute"):
                if command.action == "mute":
                    applied_mute = playback.pause_for_mute(sync=True, percent=volume_percent)
                else:
                    target = volume_percent or DEFAULT_UNMUTE_VOLUME_PERCENT
                    applied_mute = playback.resume_after_mute(target, False)
                    volume_percent = target
                muted = command.action == "mute"
                applied_mute.add_done_callback(
                    lambda result, submitted=command: save_command_result(
                        submitted, "rejected" if result.cancelled() or result.exception() else "applied",
                    ),
                )
                changed = True
                continue
            if command.action in SESSION_ACTIONS and listening_control is not None:
                applied = listening_control(command)
                record_control_outcome(command, applied)
                changed |= bool(applied)
                continue
            save_command_result(command, "rejected")
        except (PlaybackError, OSError) as error:
            # The batch has already left the queue. A failed command must not discard the rest.
            print(f"Web player command {command.action} failed: {type(error).__name__}")
            save_command_result(command, "rejected")
    return volume_percent, muted, changed, refresh_library


def run_oled_service(
    display: Sh1122Display,
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    status_refresh_seconds: float = DEFAULT_STATUS_REFRESH_SECONDS,
    splash_seconds: float = DEFAULT_SPLASH_SECONDS,
    startup_network_grace_seconds: float = DEFAULT_STARTUP_NETWORK_GRACE_SECONDS,
    home_refresh_seconds: float = DEFAULT_HOME_REFRESH_SECONDS,
    nav_input_enabled: bool = True,
    nav_pins: Ky040Pins = Ky040Pins(clk=5, dt=6, sw=13, reversed=True),
    volume_input_enabled: bool = True,
    volume_pins: Ky040Pins = Ky040Pins(clk=12, dt=16, sw=26, reversed=True),
    mixer_controls: tuple[str, ...] = DEFAULT_MIXER_CONTROLS,
    screen_saver_idle_seconds: float = DEFAULT_SCREEN_SAVER_IDLE_SECONDS,
    screen_saver_frames_per_second: float = DEFAULT_SCREEN_SAVER_FRAMES_PER_SECOND,
    amp_shutdown_pin: int | None = None,
    software_volume_only: bool = True,
) -> None:
    enable_stack_dump_signal()
    shutdown = Shutdown()
    signal.signal(signal.SIGTERM, shutdown.request)
    signal.signal(signal.SIGINT, shutdown.request)
    audio_test = AudioOutputTest()
    progress_sync = ProgressSync()
    listening_details = ListeningDetailsPublisher()
    web_status = WebStatusPublisher(save_web_player_status)
    web_commands = WebCommandInbox(consume_web_player_commands)
    log_installed_build("OLED")
    gst_runtime = GStreamerRuntime()
    playback = PlaybackOwner(
        GStreamerPlayback(progress_sync=progress_sync, runtime=gst_runtime), startup_reporter=record_startup_sample,
    )
    spoken_navigation = SpokenNavigationFeedback(playback.set_spoken_navigation_ducked)
    amp_shutdown: AmpShutdownPin | None = None
    service_started_at = time.monotonic()
    saved_output_volumes = None

    menu = None
    shelf = OledShelf()
    dial_timing = DialTiming()
    panel_off = False

    def show(frame: DisplayFrame) -> None:
        dial_timing.show(SCREEN_OFF if panel_off else frame, display.show)

    try:
        while not shutdown.requested:
            try:
                startup_config = load_config()
                break
            except ConfigReadError:
                display.show(TwoLineFrame("Settings unreadable", "Check config.json"))
                sleep_until_shutdown(shutdown, 5.0)
        else:
            return
        panel_off = screen_off_due(startup_config, service_started_at, datetime.now(), service_started_at, None)
        show(splash_frame())
        sleep_until_shutdown(shutdown, splash_seconds)

        events = OledInputQueue()
        # Keep the rotary inputs alive while they deliver GPIO callbacks.
        _nav_input: NavigationRotaryInput | None = None
        if nav_input_enabled:
            try:
                _nav_input = NavigationRotaryInput(events, nav=nav_pins)
                print(
                    "Navigation rotary enabled on "
                    f"CLK GPIO{nav_pins.clk}, DT GPIO{nav_pins.dt}, SW GPIO{nav_pins.sw}, "
                    f"reversed={nav_pins.reversed}"
                )
            except Exception as error:
                print(f"Navigation rotary could not start: {error}")

        _volume_input: VolumeRotaryInput | None = None
        if volume_input_enabled:
            try:
                _volume_input = VolumeRotaryInput(events, volume=volume_pins)
                print(
                    "Volume rotary enabled on "
                    f"CLK GPIO{volume_pins.clk}, DT GPIO{volume_pins.dt}, SW GPIO{volume_pins.sw}"
                )
            except Exception as error:
                print(f"Volume rotary could not start: {error}")

        saved_volume = load_saved_volume() if software_volume_only else None
        if saved_volume is None:
            previous_status = load_web_player_status() if software_volume_only else WebPlayerStatus()
            initial_volume = VolumeState(previous_status.volume_percent, previous_status.muted)
        else:
            initial_volume = saved_volume
        software_volume = initial_volume.percent
        software_muted = initial_volume.muted
        volume_output = "speaker"
        if software_volume_only:
            saved_output_volumes = OutputVolumes(initial_volume, load_bluetooth_volume(initial_volume))
            playback.configure_output_volumes(saved_output_volumes)
        else:
            playback.set_volume_state(software_volume, software_muted)
        control_click_feedback = ControlClickFeedback()

        def announce_selection(selection: SpokenSelection) -> None:
            spoken_navigation.request(
                selection.text,
                delay_seconds=selection.delay_seconds,
                volume_percent=runtime_config.spoken_navigation_volume_percent,
            )

        def update_amp(*, feedback_active: bool = False) -> None:
            nonlocal amp_shutdown
            try:
                set_amp_shutdown(
                    amp_shutdown, software_muted, software_volume, feedback_active=feedback_active
                )
            except Exception as error:
                amp_shutdown = None
                print(f"MAX98357 amp shutdown control failed: {error}")

        def play_click() -> None:
            control_click_feedback.click(
                runtime_config.control_click_volume_percent,
                enable_output=lambda: update_amp(feedback_active=True),
            )

        if software_volume_only:
            print("Software-only volume control enabled")
        if amp_shutdown_pin is not None:
            try:
                amp_shutdown = AmpShutdownPin(amp_shutdown_pin)
                set_amp_shutdown(amp_shutdown, muted=software_muted, volume_percent=software_volume)
                print(f"MAX98357 amp shutdown enabled on GPIO{amp_shutdown_pin}")
            except Exception as error:
                amp_shutdown = None
                print(f"MAX98357 amp shutdown could not start: {error}")

        runtime_config = startup_config
        playback.set_playback_rate(startup_config.playback_speed)
        catalogue_books = load_cached_books(startup_config)
        menu = ApplianceMenu(
            sorted_books_for_menu(books_with_downloads(startup_config, catalogue_books), startup_config.library_sort_mode),
            library_sort_mode=startup_config.library_sort_mode,
            browsing=True,
        )
        menu.set_bluetooth_enabled(False)
        menu.set_bluetooth_connected(False)
        saved_playback = load_player_state()
        if saved_playback and saved_playback.account and saved_playback.account != progress_account(startup_config):
            saved_playback = None
        menu.set_startup_resume(saved_playback)
        cached_home_frame: DisplayFrame | None = None
        network_status = DeviceStatusCache(
            None, WifiStatus(False, message="Wi-Fi status unavailable"),
            ttl=status_refresh_seconds, name="chapter-oled-wifi",
        )
        home_frame_loaded_at = 0.0
        catalogue = OledCatalogue(home_refresh_seconds)
        overlay_frame: DisplayFrame | None = None
        displayed_opening_key = ""
        overlay_until = 0.0
        bluetooth_enabled = False
        bluetooth_connected = False
        bluetooth_used = False
        bluetooth_results: queue.SimpleQueue[BluetoothActionResult] = queue.SimpleQueue()
        bluetooth_status_results: queue.SimpleQueue[BluetoothStatusResult] = queue.SimpleQueue()
        start_bluetooth_status_worker(bluetooth_status_results)
        last_input_at = time.monotonic()
        last_loop_at = last_input_at
        last_runtime_config_load = last_input_at
        last_software_update_load = 0.0
        software_update_active = False
        software_update_display_ready = False
        software_update_screen_visible = False
        sleep_timer = ListeningSleepTimer()
        bedtime = BedtimeTimer()
        sleep_gain = 1.0
        night_volume_limit = 100
        bookmark_requests: list[Future[bool]] = []
        completed_session = None
        bookmark_picker = OledBookmarkPicker()
        last_knob_selection = 0.0
        podcast_request: Future[list[Book]] | None = None
        podcast_config: AppConfig | None = None
        display_brightness_percent = DEFAULT_ACTIVE_BRIGHTNESS_PERCENT
        screen_saver_visible = False
        wake_input_quiet_until = 0.0
        last_playback_volume_state = (software_volume, software_muted)
        last_playback_state_save = 0.0
        last_is_playing = playback.is_playing
        first_menu_drawn = False
        while not shutdown.requested:
            try:
                now = time.monotonic()
                full_loop_elapsed = max(0.0, now - last_loop_at)
                loop_elapsed = min(full_loop_elapsed, 1.0)
                last_loop_at = now
                local_time = datetime.now()
                off_due = screen_off_due(runtime_config, now, local_time, service_started_at, events.last_dial_at)
                woke_panel = panel_off and events.consume_panel_wake()
                # A gesture already queued while the screen was lit still belongs to the menu.
                panel_off = off_due and not woke_panel and (panel_off or events.empty())
                if now - last_software_update_load >= DEFAULT_SOFTWARE_UPDATE_REFRESH_SECONDS:
                    software_update_state = load_update_state()
                    software_update_active = (
                        software_update_state.phase == "installing" and update_state_is_active(software_update_state)
                    )
                    software_update_display_ready = software_update_state.display_ready
                    last_software_update_load = now
                if software_update_active:
                    drain_input_events(events)
                    spoken_navigation.cancel()
                    screen_saver_visible = False
                    display_brightness_percent = update_display_brightness(
                        display,
                        DEFAULT_ACTIVE_BRIGHTNESS_PERCENT,
                        display_brightness_percent,
                    )
                    show(SOFTWARE_UPDATE_FRAME)
                    if not software_update_screen_visible:
                        software_update_screen_visible = True
                        playback.cancel_book_request()
                        pause_and_publish_playback(
                            playback,
                            menu,
                            software_volume,
                            software_muted,
                            publisher=web_status,
                        )
                    if not software_update_display_ready:
                        try:
                            software_update_display_ready = mark_update_display_ready()
                        except OSError as error:
                            print(f"Software update display acknowledgement failed: {error}")
                    sleep_until_shutdown(shutdown, refresh_seconds)
                    continue
                software_update_screen_visible = False
                status = network_status.current(wifi_status)
                if status is not None:
                    menu.set_offline(not status.connected or status.ssid == SETUP_HOTSPOT_SSID)
                refresh_due = now - last_runtime_config_load >= DEFAULT_RUNTIME_CONFIG_REFRESH_SECONDS
                if refresh_due:
                    refreshed_config = load_config()
                    if refreshed_config.playback_speed != runtime_config.playback_speed:
                        try:
                            playback.set_playback_rate(refreshed_config.playback_speed)
                        except PlaybackError as error:
                            print(f"Playback speed update failed: {error}")
                    account_changed = progress_account(refreshed_config) != progress_account(runtime_config)
                    if account_changed or refreshed_config.library_id != runtime_config.library_id:
                        catalogue_books = load_cached_books(refreshed_config)
                        episodes = [] if account_changed else [book for book in menu.books if book.episode_id]
                        menu.set_library_sort_mode(refreshed_config.library_sort_mode)
                        menu.set_books(sorted_books_for_menu(
                            books_with_downloads(refreshed_config, [*catalogue_books, *episodes]),
                            refreshed_config.library_sort_mode,
                        ))
                    if catalogue_request_key(refreshed_config) != catalogue_request_key(runtime_config):
                        catalogue.invalidate()
                    runtime_config = refreshed_config
                    last_runtime_config_load = now
                shelf.apply_ready(runtime_config, catalogue_books, menu)
                if refresh_due:
                    shelf.request(runtime_config, catalogue_books, menu)
                progress_sync.pump(runtime_config)
                volume_snapshot = playback.state
                output_changed = volume_snapshot.output_volumes is not None and volume_snapshot.volume_output != volume_output
                if output_changed:
                    volume_output = volume_snapshot.volume_output
                    output_volume = volume_snapshot.output_volumes.for_output(volume_output)
                    software_volume, software_muted = output_volume.percent, output_volume.muted
                    last_playback_volume_state = (software_volume, software_muted)
                menu.prefer_downloaded = runtime_config.prefer_downloaded
                menu.skip_back_seconds = runtime_config.skip_back_seconds
                menu.skip_forward_seconds = runtime_config.skip_forward_seconds
                night = night_mode_active(
                    runtime_config.night_mode, runtime_config.night_start, runtime_config.night_end, local_time
                )
                active_brightness = runtime_config.night_dim_percent if night else DEFAULT_ACTIVE_BRIGHTNESS_PERCENT
                volume_limit = runtime_config.night_volume_limit if night else 100
                if volume_limit != night_volume_limit:
                    playback.set_night_volume_limit(volume_limit)
                    night_volume_limit = volume_limit
                if panel_off or woke_panel:
                    woke_screen_saver, wake_input_quiet_until = False, 0.0
                else:
                    woke_screen_saver, wake_input_quiet_until = consume_screen_saver_wake_input(
                        events, screen_saver_visible, now, wake_input_quiet_until,
                    )
                suppress_wake_input = panel_off or woke_screen_saver or now < wake_input_quiet_until
                if woke_screen_saver or woke_panel:
                    screen_saver_visible = False
                    last_input_at = now
                    if bedtime.sleeping:
                        bedtime.cancel()
                    if sleep_timer.wake():
                        menu.dismiss_sleep_confirmation()
                    display_brightness_percent = update_display_brightness(
                        display,
                        active_brightness,
                        display_brightness_percent,
                    )
                if suppress_wake_input:
                    overlay = None
                    menu_changed = False
                    commands = []
                    pause_playback = False
                    resume_playback = False
                else:
                    (
                        overlay,
                        software_volume,
                        software_muted,
                        menu_changed,
                        commands,
                        pause_playback,
                        resume_playback,
                    ) = handle_oled_input_events(
                        events,
                        menu,
                        opening_key=displayed_opening_key,
                        on_turn=dial_timing.observe,
                        mixer_controls=mixer_controls,
                        fallback_percent=software_volume,
                        fallback_muted=software_muted,
                        software_volume_only=software_volume_only,
                        bluetooth_enabled=bluetooth_enabled,
                        bluetooth_connected=bluetooth_connected,
                        control_click_feedback=(
                            play_click if runtime_config.control_click_enabled else None
                        ),
                        spoken_navigation_feedback=(
                            announce_selection if runtime_config.spoken_navigation_enabled else None
                        ),
                        spoken_navigation_cancel=(
                            spoken_navigation.cancel
                            if runtime_config.spoken_navigation_enabled
                            else None
                        ),
                    )
                menu_changed = menu_changed or output_changed
                commands, software_volume, software_muted, unmuted_for_play, resume_from_muted_play = (
                    unmute_for_audible_playback_commands(
                        commands,
                        software_volume,
                        software_muted,
                        mixer_controls=mixer_controls,
                        software_volume_only=software_volume_only,
                    )
                )
                if unmuted_for_play:
                    resume_playback = resume_playback or resume_from_muted_play
                    menu_changed = True
                bluetooth_used = bluetooth_used or menu.speakers.active or any(
                    command.type == CommandType.SET_BLUETOOTH_ENABLED for command in commands
                )
                while True:
                    try:
                        bluetooth_status = bluetooth_status_results.get_nowait()
                    except queue.Empty:
                        break
                    if bluetooth_used:
                        continue  # A startup probe can finish after the listener has changed the adapter.
                    bluetooth_enabled = bluetooth_status.enabled
                    bluetooth_connected = bluetooth_status.connected
                    menu.set_bluetooth_enabled(bluetooth_enabled)
                    menu.set_bluetooth_connected(bluetooth_connected)
                    playback.set_bluetooth_output_enabled(bluetooth_connected)
                    playback.refresh_audio_output()
                    menu_changed = True
                if menu.screen == Screen.BLUETOOTH_DEVICES:
                    speaker_result = menu.speakers.poll()
                    if menu.speakers.powered:
                        bluetooth_enabled = True
                        menu.set_bluetooth_enabled(True)
                    if speaker_result is not None:
                        bluetooth_results.put(speaker_result)
                elif menu.speakers.active:
                    menu.speakers.close()
                while True:
                    try:
                        bluetooth_result = bluetooth_results.get_nowait()
                    except queue.Empty:
                        break
                    bluetooth_enabled = bluetooth_result.enabled if bluetooth_result.enabled is not None else bluetooth_enabled
                    if bluetooth_result.connected is not None:
                        bluetooth_connected = bluetooth_result.connected
                    elif bluetooth_result.enabled is False:
                        bluetooth_connected = False
                    menu.set_bluetooth_enabled(bluetooth_enabled)
                    menu.set_bluetooth_connected(bluetooth_connected)
                    if bluetooth_result.success:
                        if bluetooth_result.device_address and bluetooth_result.connected:
                            audio_test.bluetooth_address = bluetooth_result.device_address
                            playback.set_bluetooth_output_enabled(True, bluetooth_result.device_address)
                        else:
                            if not bluetooth_connected:
                                audio_test.bluetooth_address = ""
                            playback.set_bluetooth_output_enabled(bluetooth_connected)
                        playback.refresh_audio_output()
                    menu_changed = True
                    if bluetooth_result.device_address and menu.screen == Screen.BLUETOOTH_DEVICES:
                        menu.close_bluetooth_menu()
                    overlay = bluetooth_result_frame(bluetooth_result)
                playback_volume_state = (software_volume, software_muted)
                if playback_volume_state != last_playback_volume_state and not resume_playback:
                    playback.set_volume_state(
                        software_volume, software_muted, output=volume_output if software_volume_only else None,
                    )
                    if not pause_playback:
                        web_status.publish(playback, software_volume, software_muted)
                    last_playback_volume_state = playback_volume_state
                update_amp(
                    feedback_active=control_click_feedback.needs_audio_output
                    or (
                        runtime_config.spoken_navigation_enabled
                        and spoken_navigation.needs_audio_output
                    )
                )
                input_activity = input_activity_seen(
                    overlay,
                    menu_changed,
                    commands,
                    pause_playback or resume_playback,
                )
                if input_activity:
                    last_input_at = now
                    menu.note_input_activity()
                    if sleep_timer.wake():
                        menu.dismiss_sleep_confirmation()
                        menu_changed = True
                    display_brightness_percent = update_display_brightness(
                        display,
                        active_brightness,
                        display_brightness_percent,
                    )
                menu.tick_section_letter(loop_elapsed)
                if pause_playback and not resume_playback:
                    playback.pause_for_mute()
                    menu.update_playback(playback.current_time, False)
                    web_status.publish(playback, software_volume, software_muted)
                    menu_changed = True
                elif resume_playback:
                    playback.resume_after_mute(software_volume, software_muted)
                    web_status.publish(playback, software_volume, software_muted)
                    last_playback_volume_state = playback_volume_state
                elif unmuted_for_play:
                    playback.clear_mute_pause()
                if menu_changed:
                    overlay_frame = None
                    cached_home_frame = None
                    home_frame_loaded_at = now
                for command in commands:
                    if command.type == CommandType.PLAY_BOOK:
                        input_age = max(0.0, time.monotonic() - command.selected_at) if command.selected_at is not None else 0
                        last_knob_selection = time.time() - input_age
                    if command.type == CommandType.RETRY_LIBRARY:
                        catalogue.refresh_when_online()
                        menu.library_error = None
                        continue
                    if command.type == CommandType.CONFIRM_STILL_LISTENING:
                        sleep_timer.confirm()
                    if command.type == CommandType.SET_BEDTIME:
                        overlay = arm_bedtime(bedtime, command.timer_minutes, now, menu, runtime_config.sleep_rewind)
                        playback.set_sleep_boundary(bedtime.chapter_end)
                        continue
                    if command.type == CommandType.LOAD_PODCASTS:
                        if podcast_request is None:
                            podcast_config = runtime_config
                            podcast_request = request_podcast_catalogue(podcast_config)
                        overlay = TwoLineFrame("Loading podcasts", "Please wait")
                        continue
                    if command.type == CommandType.LOAD_BOOKMARKS:
                        if bookmark_picker.open(runtime_config, menu):
                            overlay_frame = None
                            menu_changed = True
                        continue
                    if command.type == CommandType.SAVE_BOOKMARK:
                        bookmark_requests.append(playback.save_bookmark())
                        overlay = TwoLineFrame("Saving bookmark", "Audiobookshelf")
                        continue
                    command_overlay = handle_menu_command(command, menu, playback, bluetooth_results)
                    if command.type in (
                        CommandType.PAUSE_PLAYBACK,
                        CommandType.END_SELECTION,
                        CommandType.TOGGLE_PLAYBACK,
                        CommandType.SEEK_CHAPTER,
                        CommandType.HOME,
                        CommandType.CONFIRM_STILL_LISTENING,
                    ):
                        web_status.publish(playback, software_volume, software_muted)
                    menu_changed = True
                    cached_home_frame = None
                    home_frame_loaded_at = now
                    if command_overlay is not None:
                        overlay = command_overlay
                catalogue_requests: list[WebPlayerCommand] = []

                def request_web_book(web_command: WebPlayerCommand) -> None:
                    nonlocal overlay, menu_changed
                    replacement = handle_web_book_request(web_command, last_knob_selection, menu, playback)
                    if replacement is not None:
                        overlay = replacement
                    menu_changed = True

                def set_web_sleep_timer(minutes: int) -> None:
                    nonlocal overlay
                    overlay = arm_bedtime(bedtime, minutes, now, menu, runtime_config.sleep_rewind)
                    playback.set_sleep_boundary(bedtime.chapter_end)

                web_volume, web_muted, web_changed, web_refresh_library = handle_web_player_commands(
                    playback,
                    menu,
                    software_volume,
                    software_muted,
                    commands=web_commands.read(),
                    request_catalogue=catalogue_requests.append,
                    audio_test=audio_test,
                    night_volume_limit=night_volume_limit,
                    request_book=request_web_book,
                    listening_control=lambda command: apply_listening_control(
                        command, playback, menu, runtime_config, set_web_sleep_timer,
                    ),
                )
                if web_changed:
                    software_volume = web_volume
                    software_muted = web_muted
                    last_playback_volume_state = (software_volume, software_muted)
                    menu_changed = True
                    cached_home_frame = None
                    home_frame_loaded_at = now
                    if playback.is_playing and bedtime.sleeping:
                        bedtime.cancel()
                    if playback.is_playing and (sleep_timer.prompt_active or sleep_timer.sleeping):
                        sleep_timer.confirm()
                        menu.dismiss_sleep_confirmation()
                        last_input_at = now
                        display_brightness_percent = update_display_brightness(
                            display,
                            active_brightness,
                            display_brightness_percent,
                        )
                output_volumes = playback.state.output_volumes
                if output_volumes is not None and output_volumes != saved_output_volumes:
                    try:
                        save_output_volumes(output_volumes)
                        saved_output_volumes = output_volumes
                    except OSError as error:
                        print(f"Could not save playback volume: {error}")
                if web_refresh_library:
                    catalogue.invalidate()
                if catalogue_requests:
                    catalogue.request_latest(catalogue_requests, load_config(), now)
                refreshed = catalogue.poll()
                if refreshed:
                    book_result, effective_config = refreshed
                    if book_result.books is not None and book_result.library is not None:
                        runtime_config = apply_library_choice(
                            runtime_config, effective_config.library_id, effective_config.library_name
                        )
                    cached_home_frame = None
                    home_frame_loaded_at = now
                    if book_result.books is not None:
                        catalogue_books = book_result.books
                        menu.set_library_sort_mode(effective_config.library_sort_mode)
                        menu.set_books(sorted_books_for_menu(
                            books_with_downloads(effective_config, [
                                *catalogue_books, *(book for book in menu.books if book.episode_id)
                            ]), effective_config.library_sort_mode,
                        ))
                    elif book_result.error_frame is not None and not menu.books:
                        menu.library_error = book_result.error_frame
                playback_result = playback.advance_book_request()
                if not playback.loading_book:
                    if overlay_frame == BOOK_LOADING_FRAME:
                        overlay_frame = None
                    if overlay == BOOK_LOADING_FRAME:
                        overlay = None
                if playback_result is not None:
                    overlay_frame = None
                    cached_home_frame = None
                    home_frame_loaded_at = now
                    if playback_result.error_frame is not None:
                        overlay = playback_result.error_frame
                    elif playback_result.session is not None:
                        menu.set_session(playback_result.session, is_playing=playback.is_playing, session_key=playback.state.session_key)
                        remove_started_book(runtime_config, playback_result.session)
                        save_playback_state(playback)
                        last_playback_state_save = now
                        web_status.publish(playback, software_volume, software_muted)
                if bookmark_picker.apply_ready(runtime_config, menu):
                    menu_changed = True
                for request in bookmark_requests[:]:
                    if request.done():
                        bookmark_requests.remove(request)
                        overlay = (
                            TwoLineFrame("Bookmark saved", "Audiobookshelf")
                            if request.result()
                            else TwoLineFrame("Bookmark failed", "Check connection")
                        )
                if podcast_request and podcast_request.done():
                    finished_request = podcast_request
                    podcast_request = None
                    if podcast_config and (
                        progress_account(podcast_config) == progress_account(runtime_config)
                        and podcast_config.podcasts == runtime_config.podcasts
                    ):
                        try:
                            episodes = finished_request.result()
                        except Exception as error:
                            print(f"Podcast catalogue request failed: {type(error).__name__}")
                            overlay = TwoLineFrame("Podcasts unavailable", "Open Podcasts to retry")
                        else:
                            menu.set_books([*(book for book in menu.books if not book.episode_id), *episodes])
                            overlay = None if episodes else TwoLineFrame("No episodes", "Check podcast library")
                            overlay_frame = None
                if runtime_config.spoken_navigation_enabled:
                    speech_output_device = playback.state.audio_output_device
                    if (
                        bluetooth_connected
                        and not speech_output_device
                        and spoken_navigation.ready_to_start
                    ):
                        speech_output_device = preferred_bluetooth_alsa_device(audio_test.bluetooth_address)
                    spoken_navigation.poll(speech_output_device)
                else:
                    spoken_navigation.cancel()
                if playback.loading_book and playback.state.completed_session is not None:
                    completed_session = playback.state.completed_session
                if not playback.loading_book:
                    playback_error_frame = poll_playback_safely(playback)
                    if playback_error_frame is not None:
                        overlay = playback_error_frame
                        menu_changed = True
                        cached_home_frame = None
                        home_frame_loaded_at = now
                    finished = playback.state.completed_session
                    if finished is not None and finished is not completed_session:
                        completed_session = finished
                        can_advance = not software_muted and not bedtime.sleeping and (
                            bedtime.deadline is None or now < bedtime.deadline
                        )
                        next_book = finish_naturally(menu, runtime_config, finished, can_advance=can_advance)
                        if next_book is not None:
                            overlay = handle_play_book_command(MenuCommand(
                                CommandType.PLAY_BOOK, book_id=next_book.id,
                                resume_time=next_book.current_time,
                            ), menu, playback)
                        menu_changed = True
                        cached_home_frame = None
                    if playback.session is not None:
                        menu.update_playback(playback.current_time, playback.is_playing)
                        if now - last_playback_state_save >= DEFAULT_PLAYBACK_STATE_SAVE_SECONDS:
                            save_playback_state(playback)
                            last_playback_state_save = now
                        menu.update_title_scroll(
                            loop_elapsed,
                            delay_seconds=DEFAULT_TITLE_SCROLL_DELAY_SECONDS,
                            pixels_per_second=DEFAULT_TITLE_SCROLL_PX_PER_SECOND,
                        )
                        if menu.tick_chapter_confirm_timeout(loop_elapsed):
                            cached_home_frame = None
                            home_frame_loaded_at = now
                        if menu.tick_home_return_timeout(loop_elapsed):
                            cached_home_frame = None
                            home_frame_loaded_at = now
                if overlay is not None:
                    overlay_frame = overlay
                    overlay_seconds = (
                        60.0
                        if overlay == BOOK_LOADING_FRAME
                        else DEFAULT_IP_OVERLAY_SECONDS
                        if isinstance(overlay, TwoLineFrame)
                        else DEFAULT_VOLUME_OVERLAY_SECONDS
                    )
                    overlay_until = now + overlay_seconds
                if playback.loading_book and overlay_frame is None:
                    overlay_frame = BOOK_LOADING_FRAME
                    overlay_until = now + 60.0

                session_id = ""
                if (session := playback.session) is not None:
                    session_id = session.id or session.library_item_id
                sleep_update = bedtime.update(
                    session_id=session_id, now=now, position=playback.current_time,
                    speed=playback.state.playback_rate, playing=playback.is_playing,
                )
                listening_details.publish(playback.state, menu.undo_position, bedtime, menu.selected_series)
                if abs(sleep_update.gain - sleep_gain) > 0.001:
                    playback.set_sleep_gain(sleep_update.gain)
                    sleep_gain = sleep_update.gain
                if sleep_update.pause:
                    playback.sleep_at_position(sleep_update.rewind_to)
                    save_playback_state(playback)
                    last_playback_state_save = now
                    menu.update_playback(playback.current_time, False)
                    sleep_timer.confirm()
                    overlay_frame = None
                    overlay_until = 0.0
                sleep_event = sleep_timer.update(
                    now=now,
                    elapsed_seconds=full_loop_elapsed,
                    enabled=runtime_config.sleep_timer_enabled and not bedtime.session_id,
                    minutes=runtime_config.sleep_timer_minutes,
                    session_id=session_id,
                    is_playing=playback.is_playing,
                )
                apply_sleep_timer_event(
                    sleep_event,
                    sleep_timer,
                    now,
                    playback,
                    menu,
                    software_volume,
                    software_muted,
                    publisher=web_status,
                )
                if sleep_event == "prompt":
                    overlay_frame = None
                    overlay_until = 0.0
                    last_input_at = now
                if sleep_event:
                    menu_changed = True
                state = playback.state
                if state.is_playing != last_is_playing:
                    last_is_playing = state.is_playing
                    menu.update_playback(state.current_time, state.is_playing)
                    web_status.publish(playback, software_volume, software_muted)

                startup_waiting = now - service_started_at < startup_network_grace_seconds

                if overlay_frame is not None and now < overlay_until:
                    visible_overlay = overlay_frame
                    display_brightness_percent = update_display_brightness(
                        display,
                        active_brightness,
                        display_brightness_percent,
                    )
                    screen_saver_visible = False
                    show(
                        frame_with_status_indicators(
                            visible_overlay,
                            software_muted or software_volume <= 0,
                            bluetooth_connected,
                        )
                    )
                    displayed_opening_key = playback.opening_key if visible_overlay == BOOK_LOADING_FRAME else ""
                    web_status.publish(playback, software_volume, software_muted, display_drawn=True)
                    sleep_until_shutdown(shutdown, refresh_seconds, wake_on_input=events.wait_for_input)
                    continue
                overlay_frame = None
                displayed_opening_key = ""

                if status is not None and status.connected and status.ssid != SETUP_HOTSPOT_SSID:
                    loading_config = catalogue.start_if_due(now)
                    if loading_config is not None:
                        menu.set_library_sort_mode(loading_config.library_sort_mode)
                    next_home_frame = menu.render()
                    if menu.screen == Screen.LIBRARY and not menu.books:
                        if not runtime_config.is_ready:
                            next_home_frame = setup_address_frame()
                        elif catalogue.in_progress:
                            next_home_frame = TwoLineFrame("Loading books", "Connecting...")
                    cached_home_frame, home_frame_loaded_at = refresh_home_cache(
                        cached_home_frame,
                        next_home_frame,
                        home_frame_loaded_at,
                        now,
                    )
                    home_frame = scrolled_home_frame(cached_home_frame, now - home_frame_loaded_at)
                    frame = frame_for_boot_state(status, home_frame, startup_waiting=startup_waiting)
                else:
                    catalogue.refresh_when_online()
                    cached_home_frame = None
                    frame = frame_for_boot_state(
                        status,
                        menu.render() if menu.books or menu.screen in (Screen.BLUETOOTH, Screen.BLUETOOTH_DEVICES) else None,
                        startup_waiting=startup_waiting,
                    )
                menu_ready = not isinstance(frame, SplashFrame)
                normal_screen_saver_active = screen_saver_is_active(
                    now,
                    last_input_at,
                    screen_saver_idle_seconds,
                ) and not sleep_timer.prompt_active
                screen_saver_active = not panel_off and (bedtime.sleeping or sleep_timer.sleeping or normal_screen_saver_active)
                if screen_saver_active:
                    screen_saver_mode = runtime_config.screen_saver_mode
                    screen_saver_started_at = (
                        bedtime.sleep_started_at or sleep_timer.sleep_started_at or last_input_at
                    )
                    frame = screen_saver_frame(
                        now,
                        screen_saver_started_at,
                        screen_saver_frames_per_second,
                        mode=screen_saver_mode,
                        words=screen_saver_words_for_mode(screen_saver_mode, menu.books),
                        brightness_percent=runtime_config.screen_saver_dim_percent,
                        clock_format=runtime_config.screen_saver_clock_format,
                    )
                brightness = (
                    min(screen_saver_contrast_percent(frame.brightness_percent), active_brightness)
                    if isinstance(frame, ScreenSaverFrame)
                    else active_brightness
                )
                display_brightness_percent = update_display_brightness(
                    display,
                    brightness,
                    display_brightness_percent,
                )
                screen_saver_visible = screen_saver_active
                show(
                    frame_with_status_indicators(
                        frame,
                        software_muted or software_volume <= 0,
                        bluetooth_connected,
                    )
                )
                if menu_ready and not first_menu_drawn:
                    first_menu_drawn = True
                    gst_runtime.start_warmup()
                    if not effective_muted(software_volume, software_muted):
                        play_boot_chime(min(software_volume, night_volume_limit))
                web_status.publish(playback, software_volume, software_muted, display_drawn=True)
                sleep_until_shutdown(shutdown, refresh_seconds, wake_on_input=events.wait_for_input)
            except ConfigReadError:
                show(TwoLineFrame("Settings unreadable", "Check config.json"))
                sleep_until_shutdown(shutdown, 5.0)
            except Exception:
                print("OLED service loop error:")
                traceback.print_exc()
                try:
                    screen_saver_visible = False
                    show(TwoLineFrame("OLED error", "See log"))
                except Exception:
                    print("OLED error frame could not be displayed:")
                    traceback.print_exc()
                sleep_until_shutdown(shutdown, 5.0)
    finally:
        with ExitStack() as cleanup:
            cleanup.callback(shelf.close)
            cleanup.callback(display.close)
            if menu is not None:
                cleanup.callback(menu.speakers.shutdown)
            cleanup.callback(close_oled_playback, playback, saved_output_volumes)
            if amp_shutdown is not None:
                cleanup.callback(amp_shutdown.close)
            cleanup.callback(spoken_navigation.close)
            gst_runtime.stop_preloading()
            if menu is not None:
                menu.speakers.close()


def close_oled_playback(playback: PlaybackOwner, saved_volumes: OutputVolumes | None) -> None:
    try:
        playback.close()
    finally:
        output_volumes = playback.state.output_volumes
        if output_volumes is not None and output_volumes != saved_volumes:
            try:
                save_output_volumes(output_volumes)
            except OSError as error:
                print(f"Could not save playback volume: {error}")


def handle_menu_command(
    command: MenuCommand,
    menu: ApplianceMenu,
    playback: PlaybackOwner,
    bluetooth_results: queue.SimpleQueue[BluetoothActionResult] | None = None,
) -> DisplayFrame | None:
    if command.type == CommandType.PLAY_BOOK:
        return handle_play_book_command(command, menu, playback)

    if command.type == CommandType.END_SELECTION:
        try:
            end_book_selection(playback, menu, command.selection_key)
        except OSError:
            return TwoLineFrame("Playback stopped", "Could not save place")
        return None

    if command.type == CommandType.PAUSE_PLAYBACK:
        playback.cancel_book_request()
        if playback.pause(sync=True):
            menu.update_playback(playback.current_time, playback.is_playing)
        return None

    if command.type == CommandType.TOGGLE_PLAYBACK:
        playback.cancel_book_request()
        playback.toggle()
        menu.update_playback(playback.current_time, playback.is_playing)
        return None

    if command.type == CommandType.SEEK_CHAPTER:
        previous = playback.current_time
        if playback.seek_global(command.seek_time) is False:
            menu.update_playback(previous, playback.is_playing)
            return TwoLineFrame("Could not jump", "Try again")
        menu.undo_position = previous
        playback.resume(sync=False)
        menu.update_playback(playback.current_time, playback.is_playing)
        return None

    if command.type == CommandType.SEEK_POSITION:
        if not seek_listening_position(playback, menu, command.seek_time):
            return TwoLineFrame("Could not jump", "Try again")
        return None

    if command.type == CommandType.UNDO_JUMP and menu.undo_position is not None:
        if not seek_listening_position(playback, menu, menu.undo_position, undo=True):
            return TwoLineFrame("Could not undo", "Try again")
        return None

    if command.type == CommandType.MARK_FINISHED:
        session = playback.session
        if session is None or not playback.mark_finished():
            return TwoLineFrame("Cannot mark finished", "Duration unavailable")
        finish_menu_book(menu, session)
        return TwoLineFrame("Marked finished", session.title)

    if command.type == CommandType.CONFIRM_STILL_LISTENING:
        if playback.session is not None and not playback.is_playing:
            playback.resume(sync=False)
        menu.update_playback(playback.current_time, playback.is_playing)
        return None

    if command.type == CommandType.HOME:
        playback.cancel_book_request()
        return None

    if command.type == CommandType.CLEAR_LAST_PLAYBACK:
        playback.cancel_book_request()
        clear_last_playback()
        return None

    if command.type == CommandType.SET_BLUETOOTH_ENABLED:
        menu.set_bluetooth_enabled(command.bluetooth_enabled)
        if bluetooth_results is not None:
            start_bluetooth_power_worker(command.bluetooth_enabled, bluetooth_results)
            return BLUETOOTH_POWER_FRAME
        result = set_bluetooth_power(command.bluetooth_enabled)
        menu.set_bluetooth_enabled(result.enabled if result.enabled is not None else command.bluetooth_enabled)
        if result.success:
            playback.refresh_audio_output()
        return bluetooth_result_frame(result)

    return None


def finish_naturally(
    menu: ApplianceMenu, config: AppConfig, session: PlaybackSession, *, can_advance: bool,
) -> Book | None:
    series = menu.selected_series
    if not session.episode_id and (
        session.progress_account != progress_account(config) or session.library_id != config.library_id
    ):
        menu.reset_to_library()
        return None
    finish_menu_book(menu, session)
    if not can_advance or session.episode_id or not session.progress_account:
        return None
    book = next_queued_book(read_queue(catalogue_status_key(config)), menu.books, session.library_item_id, series)
    if book is not None:
        menu.selected_series = series
    return book


def handle_web_book_request(
    command: WebPlayerCommand, last_knob_selection: float, menu: ApplianceMenu, playback: PlaybackOwner,
) -> DisplayFrame | None:
    if command.created_at <= last_knob_selection:
        save_command_result(command, "superseded")
        return None
    if not command.book_id or command.catalogue_key != catalogue_request_key(load_config()):
        save_command_result(command, "rejected")
        return None
    if time.time() - command.created_at > 45:
        save_command_result(command, "rejected")
        return None
    menu.selected_series = command.series
    return handle_play_book_command(MenuCommand(
        CommandType.PLAY_BOOK, book_id=command.book_id, start_over=command.start_over,
        resume_time=command.resume_time, local_only=command.local_only, web_command=command,
    ), menu, playback)


def handle_play_book_command(
    command: MenuCommand,
    menu: ApplianceMenu,
    playback: PlaybackOwner,
) -> DisplayFrame | None:
    starting_book = playback.starting_book
    playback.cancel_book_request()
    state = playback.state
    wants_local = command.local_only or (menu.prefer_downloaded and any(
        book.id == command.book_id and book.downloaded for book in menu.books
    ))
    if (
        not starting_book
        and should_return_to_active_session(command, state)
        and (not wants_local or state.session.downloaded)
        and (
            command.web_command is None
            or state.session.progress_account == command.web_command.catalogue_key.partition("/")[0]
        )
    ):
        if command.web_command is not None:
            try:
                playback.resume(sync=False, selection_key=command.web_command.id)
            except PlaybackError:
                save_command_result(command.web_command, "rejected")
                return TwoLineFrame("Could not play", "Try again")
            state = playback.state
            save_command_result(command.web_command, "applied" if state.is_playing else "rejected")
        menu.set_session(state.session, is_playing=state.is_playing, session_key=state.session_key)
        if state.is_playing:
            remove_started_book(load_config(), state.session)
        menu.update_playback(state.current_time, state.is_playing)
        return None

    playback.request_book(command)
    return BOOK_LOADING_FRAME


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


def should_return_to_active_session(command: MenuCommand, state: PlaybackSnapshot) -> bool:
    return (
        not command.start_over
        and state.session is not None
        and state.is_playing
        and state.session.library_item_id == command.book_id
        and state.session.episode_id == command.episode_id
    )


def sleep_until_shutdown(
    shutdown: Shutdown,
    seconds: float,
    *,
    wake_on_input: Callable[[float], bool] | None = None,
) -> None:
    deadline = time.monotonic() + seconds
    while not shutdown.requested and time.monotonic() < deadline:
        remaining = min(0.1, max(0.0, deadline - time.monotonic()))
        if wake_on_input is None:
            time.sleep(remaining)
        elif wake_on_input(remaining):
            return


def signal_boot_splash_handoff(
    path: str = DEFAULT_BOOT_SPLASH_HANDOFF_PATH,
    settle_seconds: float = DEFAULT_BOOT_SPLASH_HANDOFF_SETTLE_SECONDS,
) -> None:
    try:
        Path(path).write_text("starting\n", encoding="utf-8")
    except OSError as error:
        print(f"Boot splash handoff marker could not be written: {error}")
        return
    if settle_seconds > 0:
        time.sleep(settle_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Chapter Player for Audiobookshelf OLED status service.")
    parser.add_argument("--spi-bus", type=int, default=0)
    parser.add_argument("--spi-device", type=int, default=0)
    parser.add_argument("--dc-pin", type=int, default=24)
    parser.add_argument("--reset-pin", type=int, default=25)
    parser.add_argument("--cs-pin", type=int, default=None)
    parser.add_argument("--speed", type=int, default=1_000_000)
    parser.add_argument("--refresh-seconds", type=float, default=DEFAULT_REFRESH_SECONDS)
    parser.add_argument("--status-refresh-seconds", type=float, default=DEFAULT_STATUS_REFRESH_SECONDS)
    parser.add_argument("--splash-seconds", type=float, default=DEFAULT_SPLASH_SECONDS)
    parser.add_argument(
        "--startup-network-grace-seconds",
        type=float,
        default=DEFAULT_STARTUP_NETWORK_GRACE_SECONDS,
    )
    parser.add_argument("--home-refresh-seconds", type=float, default=DEFAULT_HOME_REFRESH_SECONDS)
    parser.add_argument("--no-nav-input", action="store_true")
    parser.add_argument("--nav-clk-pin", type=int, default=5)
    parser.add_argument("--nav-dt-pin", type=int, default=6)
    parser.add_argument("--nav-sw-pin", type=int, default=13)
    nav_direction = parser.add_mutually_exclusive_group()
    nav_direction.add_argument("--nav-normal-direction", dest="nav_reversed", action="store_false")
    nav_direction.add_argument("--nav-reversed", dest="nav_reversed", action="store_true")
    parser.set_defaults(nav_reversed=True)
    parser.add_argument("--no-volume-input", action="store_true")
    parser.add_argument("--volume-clk-pin", type=int, default=12)
    parser.add_argument("--volume-dt-pin", type=int, default=16)
    parser.add_argument("--volume-sw-pin", type=int, default=26)
    parser.add_argument("--volume-normal-direction", action="store_true")
    parser.add_argument("--mixer-control", action="append", default=None)
    parser.add_argument("--system-volume-control", action="store_true")
    parser.add_argument("--amp-shutdown-pin", type=int, default=None)
    parser.add_argument("--screen-saver-idle-seconds", type=float, default=DEFAULT_SCREEN_SAVER_IDLE_SECONDS)
    parser.add_argument("--screen-saver-fps", type=float, default=DEFAULT_SCREEN_SAVER_FRAMES_PER_SECOND)
    parser.add_argument("--no-screen-saver", action="store_true")
    parser.add_argument("--boot-splash-handoff-path", default=DEFAULT_BOOT_SPLASH_HANDOFF_PATH)
    parser.add_argument("--boot-splash-handoff-settle-seconds", type=float, default=DEFAULT_BOOT_SPLASH_HANDOFF_SETTLE_SECONDS)
    args = parser.parse_args()

    if args.boot_splash_handoff_path:
        signal_boot_splash_handoff(
            args.boot_splash_handoff_path,
            settle_seconds=args.boot_splash_handoff_settle_seconds,
        )

    display = Sh1122Display(
        spi_bus=args.spi_bus,
        spi_device=args.spi_device,
        dc_pin=args.dc_pin,
        reset_pin=args.reset_pin,
        cs_pin=args.cs_pin,
        start_off=True,
        max_speed_hz=args.speed,
    )
    run_oled_service(
        display,
        refresh_seconds=args.refresh_seconds,
        status_refresh_seconds=args.status_refresh_seconds,
        splash_seconds=args.splash_seconds,
        startup_network_grace_seconds=args.startup_network_grace_seconds,
        home_refresh_seconds=args.home_refresh_seconds,
        nav_input_enabled=not args.no_nav_input,
        nav_pins=Ky040Pins(
            clk=args.nav_clk_pin,
            dt=args.nav_dt_pin,
            sw=args.nav_sw_pin,
            reversed=args.nav_reversed,
        ),
        volume_input_enabled=not args.no_volume_input,
        volume_pins=Ky040Pins(
            clk=args.volume_clk_pin,
            dt=args.volume_dt_pin,
            sw=args.volume_sw_pin,
            reversed=not args.volume_normal_direction,
        ),
        mixer_controls=tuple(args.mixer_control or DEFAULT_MIXER_CONTROLS),
        screen_saver_idle_seconds=0.0 if args.no_screen_saver else args.screen_saver_idle_seconds,
        screen_saver_frames_per_second=args.screen_saver_fps,
        amp_shutdown_pin=args.amp_shutdown_pin,
        software_volume_only=not args.system_volume_control,
    )


if __name__ == "__main__":
    main()
