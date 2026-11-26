from __future__ import annotations

import argparse
import faulthandler
import queue
import signal
import time
import traceback
from concurrent.futures import Future
from contextlib import ExitStack
from dataclasses import dataclass
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
    preferred_bluetooth_alsa_device,
    set_bluetooth_power,
)
from .bluetooth_picker import SpeakerPicker
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
from .display_frames import (
    DisplayFrame,
    HomeFrame,
    SplashFrame,
    TwoLineFrame,
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
from .oled_bluetooth import (
    OledBluetooth,
    start_bluetooth_power_worker,
    start_bluetooth_status_worker,
)
from .oled_bluetooth import bluetooth_result_frame as bluetooth_result_frame
from .oled_bookmarks import OledBookmarkPicker
from .oled_catalogue import OledCatalogue
from .oled_display import Sh1122Display
from .oled_input import (
    OledInputQueue,
    drain_input_events,
    handle_oled_input_events,
    input_activity_seen,
)
from .oled_presentation import (
    BOOK_LOADING_FRAME,
    DEFAULT_ACTIVE_BRIGHTNESS_PERCENT,
    DEFAULT_TITLE_SCROLL_DELAY_SECONDS,
    DEFAULT_TITLE_SCROLL_PX_PER_SECOND,
    OledPresentation,
    splash_frame,
)
from .oled_presentation import frame_for_boot_state as frame_for_boot_state
from .oled_presentation import frame_for_wifi_status as frame_for_wifi_status
from .oled_presentation import frame_with_mute_indicator as frame_with_mute_indicator
from .oled_presentation import frame_with_status_indicators as frame_with_status_indicators
from .oled_presentation import refresh_home_cache as refresh_home_cache
from .oled_presentation import scrolled_home_frame as scrolled_home_frame
from .oled_presentation import update_display_brightness as update_display_brightness
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
    SpokenSelection,
    book_is_podcast,
)
from .screen_power import screen_off_due
from .screen_saver import (
    DEFAULT_SCREEN_SAVER_FRAMES_PER_SECOND,
    DEFAULT_SCREEN_SAVER_IDLE_SECONDS,
)
from .software_update import load_update_state, mark_update_display_ready, update_state_is_active
from .spoken_navigation import SpokenNavigationFeedback
from .volume_controls import (
    DEFAULT_UNMUTE_VOLUME_PERCENT,
    effective_muted,
    restored_audible_volume_state,
    unmuted_volume,
)
from .wifi import SETUP_HOTSPOT_SSID, WifiStatus, wifi_status


DEFAULT_REFRESH_SECONDS = 0.14
DEFAULT_STATUS_REFRESH_SECONDS = 2.0
DEFAULT_SPLASH_SECONDS = 0.5
DEFAULT_STARTUP_NETWORK_GRACE_SECONDS = 30.0
DEFAULT_HOME_REFRESH_SECONDS = 60.0
DEFAULT_BOOT_SPLASH_HANDOFF_PATH = "/tmp/audiobookshelf-player-oled-starting"
DEFAULT_BOOT_SPLASH_HANDOFF_SETTLE_SECONDS = 0.3
DEFAULT_PLAYBACK_STATE_SAVE_SECONDS = 5.0
DEFAULT_CONTROL_CLICK_MIN_INTERVAL_SECONDS = 0.045
CONTROL_CLICK_OUTPUT_HOLD_SECONDS = 0.15
DEFAULT_RUNTIME_CONFIG_REFRESH_SECONDS = 1.0
DEFAULT_SOFTWARE_UPDATE_REFRESH_SECONDS = 0.2
BLUETOOTH_POWER_FRAME = TwoLineFrame("Bluetooth", "Updating...")
SOFTWARE_UPDATE_FRAME = TwoLineFrame("UPDATING", "DO NOT POWER OFF")


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
    loop = OledServiceLoop(
        display,
        refresh_seconds=refresh_seconds,
        screen_saver_idle_seconds=screen_saver_idle_seconds,
        screen_saver_frames_per_second=screen_saver_frames_per_second,
        startup_network_grace_seconds=startup_network_grace_seconds,
        mixer_controls=mixer_controls,
        software_volume_only=software_volume_only,
    )
    try:
        config = wait_for_startup_config(display, shutdown)
        if config is None:
            return
        loop.show_splash(config)
        sleep_until_shutdown(shutdown, splash_seconds)
        loop.start_inputs(
            nav_pins if nav_input_enabled else None, volume_pins if volume_input_enabled else None
        )
        loop.start_audio(amp_shutdown_pin)
        loop.start_library(config, home_refresh_seconds)
        loop.start_polling(status_refresh_seconds)
        loop.run(shutdown)
    finally:
        loop.close()


def wait_for_startup_config(display: Sh1122Display, shutdown: Shutdown) -> AppConfig | None:
    while not shutdown.requested:
        try:
            return load_config()
        except ConfigReadError:
            display.show(TwoLineFrame("Settings unreadable", "Check config.json"))
            sleep_until_shutdown(shutdown, 5.0)
    return None


@dataclass
class DialActions:
    """Changes from draining the dial queue once, before web commands are applied."""

    commands: list[MenuCommand]
    overlay: DisplayFrame | None = None
    changed: bool = False
    pause: bool = False
    resume: bool = False
    unmuted_for_play: bool = False


class OledServiceLoop:
    def __init__(
        self,
        display: Sh1122Display,
        *,
        refresh_seconds: float,
        screen_saver_idle_seconds: float,
        screen_saver_frames_per_second: float,
        startup_network_grace_seconds: float,
        mixer_controls: tuple[str, ...],
        software_volume_only: bool,
    ) -> None:
        self.refresh_seconds = refresh_seconds
        self.startup_network_grace_seconds = startup_network_grace_seconds
        self.mixer_controls = mixer_controls
        self.software_volume_only = software_volume_only
        self.audio_test = AudioOutputTest()
        self.progress_sync = ProgressSync()
        self.listening_details = ListeningDetailsPublisher()
        self.web_status = WebStatusPublisher(save_web_player_status)
        self.web_commands = WebCommandInbox(consume_web_player_commands)
        log_installed_build("OLED")
        self.gst_runtime = GStreamerRuntime()
        self.playback = PlaybackOwner(
            GStreamerPlayback(progress_sync=self.progress_sync, runtime=self.gst_runtime),
            startup_reporter=record_startup_sample,
        )
        self.spoken_navigation = SpokenNavigationFeedback(
            self.playback.set_spoken_navigation_ducked
        )
        self.amp_shutdown: AmpShutdownPin | None = None
        self.started_at = time.monotonic()
        self.saved_output_volumes: OutputVolumes | None = None
        self.speakers: SpeakerPicker | None = None
        self.shelf = OledShelf()
        self.screen = OledPresentation(
            display, self.started_at, screen_saver_idle_seconds, screen_saver_frames_per_second
        )

    def show_splash(self, config: AppConfig) -> None:
        self.screen.panel_off = screen_off_due(
            config, self.started_at, datetime.now(), self.started_at, None
        )
        self.screen.show(splash_frame())

    def start_inputs(self, nav_pins: Ky040Pins | None, volume_pins: Ky040Pins | None) -> None:
        self.events = OledInputQueue()
        # Keep the rotary inputs alive while they deliver GPIO callbacks.
        self.nav_input: NavigationRotaryInput | None = None
        if nav_pins is not None:
            try:
                self.nav_input = NavigationRotaryInput(self.events, nav=nav_pins)
                print(
                    "Navigation rotary enabled on "
                    f"CLK GPIO{nav_pins.clk}, DT GPIO{nav_pins.dt}, SW GPIO{nav_pins.sw}, "
                    f"reversed={nav_pins.reversed}"
                )
            except Exception as error:
                print(f"Navigation rotary could not start: {error}")

        self.volume_input: VolumeRotaryInput | None = None
        if volume_pins is not None:
            try:
                self.volume_input = VolumeRotaryInput(self.events, volume=volume_pins)
                print(
                    "Volume rotary enabled on "
                    f"CLK GPIO{volume_pins.clk}, DT GPIO{volume_pins.dt}, SW GPIO{volume_pins.sw}"
                )
            except Exception as error:
                print(f"Volume rotary could not start: {error}")

    def start_audio(self, amp_shutdown_pin: int | None) -> None:
        saved_volume = load_saved_volume() if self.software_volume_only else None
        if saved_volume is None:
            previous_status = (
                load_web_player_status() if self.software_volume_only else WebPlayerStatus()
            )
            initial_volume = VolumeState(previous_status.volume_percent, previous_status.muted)
        else:
            initial_volume = saved_volume
        self.volume = initial_volume.percent
        self.muted = initial_volume.muted
        self.volume_output = "speaker"
        if self.software_volume_only:
            self.saved_output_volumes = OutputVolumes(
                initial_volume, load_bluetooth_volume(initial_volume)
            )
            self.playback.configure_output_volumes(self.saved_output_volumes)
        else:
            self.playback.set_volume_state(self.volume, self.muted)
        self.control_click_feedback = ControlClickFeedback()

        if self.software_volume_only:
            print("Software-only volume control enabled")
        if amp_shutdown_pin is not None:
            try:
                self.amp_shutdown = AmpShutdownPin(amp_shutdown_pin)
                set_amp_shutdown(self.amp_shutdown, muted=self.muted, volume_percent=self.volume)
                print(f"MAX98357 amp shutdown enabled on GPIO{amp_shutdown_pin}")
            except Exception as error:
                self.amp_shutdown = None
                print(f"MAX98357 amp shutdown could not start: {error}")

    def announce_selection(self, selection: SpokenSelection) -> None:
        self.spoken_navigation.request(
            selection.text,
            delay_seconds=selection.delay_seconds,
            volume_percent=self.config.spoken_navigation_volume_percent,
        )

    def update_amp(self, *, feedback_active: bool = False) -> None:
        try:
            set_amp_shutdown(
                self.amp_shutdown, self.muted, self.volume, feedback_active=feedback_active
            )
        except Exception as error:
            self.amp_shutdown = None
            print(f"MAX98357 amp shutdown control failed: {error}")

    def play_click(self) -> None:
        self.control_click_feedback.click(
            self.config.control_click_volume_percent,
            enable_output=lambda: self.update_amp(feedback_active=True),
        )

    def start_library(self, config: AppConfig, home_refresh_seconds: float) -> None:
        self.config = config
        self.playback.set_playback_rate(config.playback_speed)
        self.catalogue_books = load_cached_books(config)
        self.menu = ApplianceMenu(
            sorted_books_for_menu(
                books_with_downloads(config, self.catalogue_books), config.library_sort_mode
            ),
            library_sort_mode=config.library_sort_mode,
            browsing=True,
        )
        self.speakers = self.menu.speakers
        self.menu.set_bluetooth_enabled(False)
        self.menu.set_bluetooth_connected(False)
        saved_playback = load_player_state()
        if (
            saved_playback
            and saved_playback.account
            and saved_playback.account != progress_account(config)
        ):
            saved_playback = None
        self.menu.set_startup_resume(saved_playback)
        self.catalogue = OledCatalogue(home_refresh_seconds)

    def start_polling(self, status_refresh_seconds: float) -> None:
        self.network_status = DeviceStatusCache(
            None,
            WifiStatus(False, message="Wi-Fi status unavailable"),
            ttl=status_refresh_seconds,
            name="chapter-oled-wifi",
        )
        self.bluetooth = OledBluetooth()
        start_bluetooth_status_worker(self.bluetooth.status_results)
        self.screen.last_input_at = time.monotonic()
        self.last_loop_at = self.screen.last_input_at
        self.last_runtime_config_load = self.screen.last_input_at
        self.last_software_update_load = 0.0
        self.software_update_active = False
        self.software_update_display_ready = False
        self.software_update_screen_visible = False
        self.sleep_timer = ListeningSleepTimer()
        self.bedtime = BedtimeTimer()
        self.sleep_gain = 1.0
        self.night_volume_limit = 100
        self.bookmark_requests: list[Future[bool]] = []
        self.completed_session = None
        self.bookmark_picker = OledBookmarkPicker()
        self.last_knob_selection = 0.0
        self.podcast_request: Future[list[Book]] | None = None
        self.podcast_config: AppConfig | None = None
        self.last_volume_state = (self.volume, self.muted)
        self.last_playback_state_save = 0.0
        self.last_is_playing = self.playback.is_playing
        self.first_menu_drawn = False

    def run(self, shutdown: Shutdown) -> None:
        while not shutdown.requested:
            try:
                self.tick()
                if self.software_update_active:
                    sleep_until_shutdown(shutdown, self.refresh_seconds)
                else:
                    sleep_until_shutdown(
                        shutdown, self.refresh_seconds, wake_on_input=self.events.wait_for_input
                    )
            except ConfigReadError:
                self.screen.show(TwoLineFrame("Settings unreadable", "Check config.json"))
                sleep_until_shutdown(shutdown, 5.0)
            except Exception:
                print("OLED service loop error:")
                traceback.print_exc()
                try:
                    self.screen.saver_visible = False
                    self.screen.show(TwoLineFrame("OLED error", "See log"))
                except Exception:
                    print("OLED error frame could not be displayed:")
                    traceback.print_exc()
                sleep_until_shutdown(shutdown, 5.0)

    def tick(self) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self.last_loop_at)
        menu_elapsed = min(elapsed, 1.0)
        self.last_loop_at = now
        local_time = datetime.now()
        woke_panel = self.screen.update_panel_power(self.config, self.events, now, local_time)
        if self.show_update_if_active(now):
            return

        status = self.network_status.current(wifi_status)
        if status is not None:
            self.menu.set_offline(not status.connected or status.ssid == SETUP_HOTSPOT_SSID)
        self.refresh_settings_and_shelf(now)
        self.progress_sync.pump(self.config)
        output_changed = self.sync_output_volume()
        brightness = self.apply_listening_preferences(local_time)
        overlay = self.handle_dials(now, menu_elapsed, woke_panel, brightness, output_changed)
        overlay = self.run_web_commands(overlay, now, brightness)
        self.poll_catalogue(now)
        overlay = self.advance_book_request(overlay, now)
        overlay = self.poll_bookmarks(overlay)
        overlay = self.poll_podcasts(overlay)
        self.poll_spoken_navigation()
        overlay = self.poll_playback(overlay, now, menu_elapsed)
        self.screen.update_overlay(overlay, now, loading=self.playback.loading_book)
        self.update_sleep_timers(now, elapsed)
        self.publish_playback_transition()
        self.draw_frame(status, now, brightness)

    def show_update_if_active(self, now: float) -> bool:
        if now - self.last_software_update_load >= DEFAULT_SOFTWARE_UPDATE_REFRESH_SECONDS:
            software_update_state = load_update_state()
            self.software_update_active = (
                software_update_state.phase == "installing"
                and update_state_is_active(software_update_state)
            )
            self.software_update_display_ready = software_update_state.display_ready
            self.last_software_update_load = now
        if not self.software_update_active:
            self.software_update_screen_visible = False
            return False
        drain_input_events(self.events)
        self.spoken_navigation.cancel()
        self.screen.saver_visible = False
        self.screen.set_brightness(DEFAULT_ACTIVE_BRIGHTNESS_PERCENT)
        self.screen.show(SOFTWARE_UPDATE_FRAME)
        if not self.software_update_screen_visible:
            self.software_update_screen_visible = True
            self.playback.cancel_book_request()
            pause_and_publish_playback(
                self.playback,
                self.menu,
                self.volume,
                self.muted,
                publisher=self.web_status,
            )
        if not self.software_update_display_ready:
            try:
                self.software_update_display_ready = mark_update_display_ready()
            except OSError as error:
                print(f"Software update display acknowledgement failed: {error}")
        return True

    def refresh_settings_and_shelf(self, now: float) -> None:
        refresh_due = now - self.last_runtime_config_load >= DEFAULT_RUNTIME_CONFIG_REFRESH_SECONDS
        if refresh_due:
            refreshed_config = load_config()
            if refreshed_config.playback_speed != self.config.playback_speed:
                try:
                    self.playback.set_playback_rate(refreshed_config.playback_speed)
                except PlaybackError as error:
                    print(f"Playback speed update failed: {error}")
            account_changed = progress_account(refreshed_config) != progress_account(self.config)
            if account_changed or refreshed_config.library_id != self.config.library_id:
                self.catalogue_books = load_cached_books(refreshed_config)
                episodes = (
                    [] if account_changed else [book for book in self.menu.books if book.episode_id]
                )
                self.menu.set_library_sort_mode(refreshed_config.library_sort_mode)
                self.menu.set_books(
                    sorted_books_for_menu(
                        books_with_downloads(refreshed_config, [*self.catalogue_books, *episodes]),
                        refreshed_config.library_sort_mode,
                    )
                )
            if catalogue_request_key(refreshed_config) != catalogue_request_key(self.config):
                self.catalogue.invalidate()
            self.config = refreshed_config
            self.last_runtime_config_load = now
        self.shelf.apply_ready(self.config, self.catalogue_books, self.menu)
        if refresh_due:
            self.shelf.request(self.config, self.catalogue_books, self.menu)

    def sync_output_volume(self) -> bool:
        volume_snapshot = self.playback.state
        output_changed = (
            volume_snapshot.output_volumes is not None
            and volume_snapshot.volume_output != self.volume_output
        )
        if output_changed:
            self.volume_output = volume_snapshot.volume_output
            output_volume = volume_snapshot.output_volumes.for_output(self.volume_output)
            self.volume, self.muted = output_volume.percent, output_volume.muted
            self.last_volume_state = (self.volume, self.muted)
        return output_changed

    def apply_listening_preferences(self, local_time: datetime) -> int:
        self.menu.prefer_downloaded = self.config.prefer_downloaded
        self.menu.skip_back_seconds = self.config.skip_back_seconds
        self.menu.skip_forward_seconds = self.config.skip_forward_seconds
        night = night_mode_active(
            self.config.night_mode, self.config.night_start, self.config.night_end, local_time
        )
        active_brightness = (
            self.config.night_dim_percent if night else DEFAULT_ACTIVE_BRIGHTNESS_PERCENT
        )
        volume_limit = self.config.night_volume_limit if night else 100
        if volume_limit != self.night_volume_limit:
            self.playback.set_night_volume_limit(volume_limit)
            self.night_volume_limit = volume_limit
        return active_brightness

    def handle_dials(
        self,
        now: float,
        menu_elapsed: float,
        woke_panel: bool,
        brightness: int,
        output_changed: bool,
    ) -> DisplayFrame | None:
        if self.consume_wake_gesture(now, woke_panel, brightness):
            actions = DialActions([])
        else:
            actions = self.read_dials()
        actions.changed |= output_changed
        self.poll_bluetooth(actions)
        self.apply_dial_volume(actions)
        self.note_dial_activity(actions, now, brightness)
        self.menu.tick_section_letter(menu_elapsed)
        self.apply_mute_transport(actions)
        if actions.changed:
            self.screen.overlay = None
            self.screen.invalidate_home(now)
        return self.run_menu_commands(actions.commands, actions.overlay, now)

    def consume_wake_gesture(self, now: float, woke_panel: bool, brightness: int) -> bool:
        woke_saver = self.screen.consume_saver_wake(self.events, now, woke_panel)
        suppress_input = self.screen.panel_off or woke_saver or now < self.screen.wake_quiet_until
        if woke_saver or woke_panel:
            self.screen.saver_visible = False
            self.screen.last_input_at = now
            if self.bedtime.sleeping:
                self.bedtime.cancel()
            if self.sleep_timer.wake():
                self.menu.dismiss_sleep_confirmation()
            self.screen.set_brightness(brightness)
        return suppress_input

    def read_dials(self) -> DialActions:
        (
            overlay,
            self.volume,
            self.muted,
            menu_changed,
            commands,
            pause_playback,
            resume_playback,
        ) = handle_oled_input_events(
            self.events,
            self.menu,
            opening_key=self.screen.opening_key,
            on_turn=self.screen.timing.observe,
            mixer_controls=self.mixer_controls,
            fallback_percent=self.volume,
            fallback_muted=self.muted,
            software_volume_only=self.software_volume_only,
            bluetooth_enabled=self.bluetooth.enabled,
            bluetooth_connected=self.bluetooth.connected,
            control_click_feedback=(self.play_click if self.config.control_click_enabled else None),
            spoken_navigation_feedback=(
                self.announce_selection if self.config.spoken_navigation_enabled else None
            ),
            spoken_navigation_cancel=(
                self.spoken_navigation.cancel if self.config.spoken_navigation_enabled else None
            ),
        )
        commands, self.volume, self.muted, unmuted_for_play, resume_from_muted_play = (
            unmute_for_audible_playback_commands(
                commands,
                self.volume,
                self.muted,
                mixer_controls=self.mixer_controls,
                software_volume_only=self.software_volume_only,
            )
        )
        if unmuted_for_play:
            resume_playback = resume_playback or resume_from_muted_play
            menu_changed = True
        return DialActions(
            commands,
            overlay=overlay,
            changed=menu_changed,
            pause=pause_playback,
            resume=resume_playback,
            unmuted_for_play=unmuted_for_play,
        )

    def poll_bluetooth(self, actions: DialActions) -> None:
        actions.changed |= self.bluetooth.apply_startup_status(
            self.menu, self.playback, actions.commands
        )
        self.bluetooth.poll_picker(self.menu)
        overlay = self.bluetooth.apply_actions(self.menu, self.playback, self.audio_test)
        if overlay is not None:
            actions.overlay = overlay
            actions.changed = True

    def apply_dial_volume(self, actions: DialActions) -> None:
        playback_volume_state = (self.volume, self.muted)
        if playback_volume_state != self.last_volume_state and not actions.resume:
            self.playback.set_volume_state(
                self.volume,
                self.muted,
                output=self.volume_output if self.software_volume_only else None,
            )
            if not actions.pause:
                self.web_status.publish(self.playback, self.volume, self.muted)
            self.last_volume_state = playback_volume_state
        self.update_amp(
            feedback_active=self.control_click_feedback.needs_audio_output
            or (self.config.spoken_navigation_enabled and self.spoken_navigation.needs_audio_output)
        )

    def note_dial_activity(self, actions: DialActions, now: float, brightness: int) -> None:
        input_activity = input_activity_seen(
            actions.overlay,
            actions.changed,
            actions.commands,
            actions.pause or actions.resume,
        )
        if input_activity:
            self.screen.last_input_at = now
            self.menu.note_input_activity()
            if self.sleep_timer.wake():
                self.menu.dismiss_sleep_confirmation()
                actions.changed = True
            self.screen.set_brightness(brightness)

    def apply_mute_transport(self, actions: DialActions) -> None:
        if actions.pause and not actions.resume:
            self.playback.pause_for_mute()
            self.menu.update_playback(self.playback.current_time, False)
            self.web_status.publish(self.playback, self.volume, self.muted)
            actions.changed = True
        elif actions.resume:
            self.playback.resume_after_mute(self.volume, self.muted)
            self.web_status.publish(self.playback, self.volume, self.muted)
            self.last_volume_state = (self.volume, self.muted)
        elif actions.unmuted_for_play:
            self.playback.clear_mute_pause()

    def run_menu_commands(
        self,
        commands: list[MenuCommand],
        overlay: DisplayFrame | None,
        now: float,
    ) -> DisplayFrame | None:
        for command in commands:
            if command.type == CommandType.PLAY_BOOK:
                input_age = (
                    max(0.0, time.monotonic() - command.selected_at)
                    if command.selected_at is not None
                    else 0
                )
                self.last_knob_selection = time.time() - input_age
            if command.type == CommandType.RETRY_LIBRARY:
                self.catalogue.refresh_when_online()
                self.menu.library_error = None
                continue
            if command.type == CommandType.CONFIRM_STILL_LISTENING:
                self.sleep_timer.confirm()
            if command.type == CommandType.SET_BEDTIME:
                overlay = arm_bedtime(
                    self.bedtime, command.timer_minutes, now, self.menu, self.config.sleep_rewind
                )
                self.playback.set_sleep_boundary(self.bedtime.chapter_end)
                continue
            if command.type == CommandType.LOAD_PODCASTS:
                if self.podcast_request is None:
                    self.podcast_config = self.config
                    self.podcast_request = request_podcast_catalogue(self.podcast_config)
                overlay = TwoLineFrame("Loading podcasts", "Please wait")
                continue
            if command.type == CommandType.LOAD_BOOKMARKS:
                if self.bookmark_picker.open(self.config, self.menu):
                    self.screen.overlay = None
                continue
            if command.type == CommandType.SAVE_BOOKMARK:
                self.bookmark_requests.append(self.playback.save_bookmark())
                overlay = TwoLineFrame("Saving bookmark", "Audiobookshelf")
                continue
            command_overlay = handle_menu_command(
                command, self.menu, self.playback, self.bluetooth.results
            )
            if command.type in (
                CommandType.PAUSE_PLAYBACK,
                CommandType.END_SELECTION,
                CommandType.TOGGLE_PLAYBACK,
                CommandType.SEEK_CHAPTER,
                CommandType.HOME,
                CommandType.CONFIRM_STILL_LISTENING,
            ):
                self.web_status.publish(self.playback, self.volume, self.muted)
            self.screen.invalidate_home(now)
            if command_overlay is not None:
                overlay = command_overlay
        return overlay

    def run_web_commands(
        self, overlay: DisplayFrame | None, now: float, brightness: int
    ) -> DisplayFrame | None:
        catalogue_requests: list[WebPlayerCommand] = []

        def request_web_book(web_command: WebPlayerCommand) -> None:
            nonlocal overlay
            replacement = handle_web_book_request(
                web_command, self.last_knob_selection, self.menu, self.playback
            )
            if replacement is not None:
                overlay = replacement

        def set_web_sleep_timer(minutes: int) -> None:
            nonlocal overlay
            overlay = arm_bedtime(self.bedtime, minutes, now, self.menu, self.config.sleep_rewind)
            self.playback.set_sleep_boundary(self.bedtime.chapter_end)

        web_volume, web_muted, web_changed, web_refresh_library = handle_web_player_commands(
            self.playback,
            self.menu,
            self.volume,
            self.muted,
            commands=self.web_commands.read(),
            request_catalogue=catalogue_requests.append,
            audio_test=self.audio_test,
            night_volume_limit=self.night_volume_limit,
            request_book=request_web_book,
            listening_control=lambda command: apply_listening_control(
                command,
                self.playback,
                self.menu,
                self.config,
                set_web_sleep_timer,
            ),
        )
        if web_changed:
            self.apply_web_control_result(web_volume, web_muted, now, brightness)
        self.persist_output_volumes()
        if web_refresh_library:
            self.catalogue.invalidate()
        if catalogue_requests:
            self.catalogue.request_latest(catalogue_requests, load_config(), now)
        return overlay

    def apply_web_control_result(
        self, volume: int, muted: bool, now: float, brightness: int
    ) -> None:
        self.volume = volume
        self.muted = muted
        self.last_volume_state = (self.volume, self.muted)
        self.screen.invalidate_home(now)
        if self.playback.is_playing and self.bedtime.sleeping:
            self.bedtime.cancel()
        if self.playback.is_playing and (
            self.sleep_timer.prompt_active or self.sleep_timer.sleeping
        ):
            self.sleep_timer.confirm()
            self.menu.dismiss_sleep_confirmation()
            self.screen.last_input_at = now
            self.screen.set_brightness(brightness)

    def persist_output_volumes(self) -> None:
        output_volumes = self.playback.state.output_volumes
        if output_volumes is not None and output_volumes != self.saved_output_volumes:
            try:
                save_output_volumes(output_volumes)
                self.saved_output_volumes = output_volumes
            except OSError as error:
                print(f"Could not save playback volume: {error}")

    def poll_catalogue(self, now: float) -> None:
        refreshed = self.catalogue.poll()
        if refreshed:
            book_result, effective_config = refreshed
            if book_result.books is not None and book_result.library is not None:
                self.config = apply_library_choice(
                    self.config, effective_config.library_id, effective_config.library_name
                )
            self.screen.invalidate_home(now)
            if book_result.books is not None:
                self.catalogue_books = book_result.books
                self.menu.set_library_sort_mode(effective_config.library_sort_mode)
                self.menu.set_books(
                    sorted_books_for_menu(
                        books_with_downloads(
                            effective_config,
                            [
                                *self.catalogue_books,
                                *(book for book in self.menu.books if book.episode_id),
                            ],
                        ),
                        effective_config.library_sort_mode,
                    )
                )
            elif book_result.error_frame is not None and not self.menu.books:
                self.menu.library_error = book_result.error_frame

    def advance_book_request(self, overlay: DisplayFrame | None, now: float) -> DisplayFrame | None:
        playback_result = self.playback.advance_book_request()
        if not self.playback.loading_book:
            if self.screen.overlay == BOOK_LOADING_FRAME:
                self.screen.overlay = None
            if overlay == BOOK_LOADING_FRAME:
                overlay = None
        if playback_result is not None:
            self.screen.overlay = None
            self.screen.invalidate_home(now)
            if playback_result.error_frame is not None:
                overlay = playback_result.error_frame
            elif playback_result.session is not None:
                self.menu.set_session(
                    playback_result.session,
                    is_playing=self.playback.is_playing,
                    session_key=self.playback.state.session_key,
                )
                remove_started_book(self.config, playback_result.session)
                save_playback_state(self.playback)
                self.last_playback_state_save = now
                self.web_status.publish(self.playback, self.volume, self.muted)
        return overlay

    def poll_bookmarks(self, overlay: DisplayFrame | None) -> DisplayFrame | None:
        self.bookmark_picker.apply_ready(self.config, self.menu)
        for request in self.bookmark_requests[:]:
            if request.done():
                self.bookmark_requests.remove(request)
                overlay = (
                    TwoLineFrame("Bookmark saved", "Audiobookshelf")
                    if request.result()
                    else TwoLineFrame("Bookmark failed", "Check connection")
                )
        return overlay

    def poll_podcasts(self, overlay: DisplayFrame | None) -> DisplayFrame | None:
        if self.podcast_request and self.podcast_request.done():
            finished_request = self.podcast_request
            self.podcast_request = None
            if self.podcast_config and (
                progress_account(self.podcast_config) == progress_account(self.config)
                and self.podcast_config.podcasts == self.config.podcasts
            ):
                try:
                    episodes = finished_request.result()
                except Exception as error:
                    print(f"Podcast catalogue request failed: {type(error).__name__}")
                    overlay = TwoLineFrame("Podcasts unavailable", "Open Podcasts to retry")
                else:
                    self.menu.set_books(
                        [*(book for book in self.menu.books if not book.episode_id), *episodes]
                    )
                    overlay = (
                        None if episodes else TwoLineFrame("No episodes", "Check podcast library")
                    )
                    self.screen.overlay = None
        return overlay

    def poll_spoken_navigation(self) -> None:
        if self.config.spoken_navigation_enabled:
            speech_output_device = self.playback.state.audio_output_device
            if (
                self.bluetooth.connected
                and not speech_output_device
                and self.spoken_navigation.ready_to_start
            ):
                speech_output_device = preferred_bluetooth_alsa_device(
                    self.audio_test.bluetooth_address
                )
            self.spoken_navigation.poll(speech_output_device)
        else:
            self.spoken_navigation.cancel()

    def poll_playback(
        self, overlay: DisplayFrame | None, now: float, menu_elapsed: float
    ) -> DisplayFrame | None:
        if self.playback.loading_book:
            if self.playback.state.completed_session is not None:
                self.completed_session = self.playback.state.completed_session
            return overlay
        playback_error_frame = poll_playback_safely(self.playback)
        if playback_error_frame is not None:
            overlay = playback_error_frame
            self.screen.invalidate_home(now)
        finished = self.playback.state.completed_session
        if finished is not None and finished is not self.completed_session:
            self.completed_session = finished
            can_advance = (
                not self.muted
                and not self.bedtime.sleeping
                and (self.bedtime.deadline is None or now < self.bedtime.deadline)
            )
            next_book = finish_naturally(self.menu, self.config, finished, can_advance=can_advance)
            if next_book is not None:
                overlay = handle_play_book_command(
                    MenuCommand(
                        CommandType.PLAY_BOOK,
                        book_id=next_book.id,
                        resume_time=next_book.current_time,
                    ),
                    self.menu,
                    self.playback,
                )
            self.screen.home_frame = None
        self.update_playing_menu(now, menu_elapsed)
        return overlay

    def update_playing_menu(self, now: float, menu_elapsed: float) -> None:
        if self.playback.session is not None:
            self.menu.update_playback(self.playback.current_time, self.playback.is_playing)
            if now - self.last_playback_state_save >= DEFAULT_PLAYBACK_STATE_SAVE_SECONDS:
                save_playback_state(self.playback)
                self.last_playback_state_save = now
            self.menu.update_title_scroll(
                menu_elapsed,
                delay_seconds=DEFAULT_TITLE_SCROLL_DELAY_SECONDS,
                pixels_per_second=DEFAULT_TITLE_SCROLL_PX_PER_SECOND,
            )
            if self.menu.tick_chapter_confirm_timeout(menu_elapsed):
                self.screen.invalidate_home(now)
            if self.menu.tick_home_return_timeout(menu_elapsed):
                self.screen.invalidate_home(now)

    def update_sleep_timers(self, now: float, elapsed: float) -> None:
        session_id = ""
        if (session := self.playback.session) is not None:
            session_id = session.id or session.library_item_id
        self.update_bedtime(session_id, now)
        self.update_listening_timer(session_id, now, elapsed)

    def update_bedtime(self, session_id: str, now: float) -> None:
        sleep_update = self.bedtime.update(
            session_id=session_id,
            now=now,
            position=self.playback.current_time,
            speed=self.playback.state.playback_rate,
            playing=self.playback.is_playing,
        )
        self.listening_details.publish(
            self.playback.state, self.menu.undo_position, self.bedtime, self.menu.selected_series
        )
        if abs(sleep_update.gain - self.sleep_gain) > 0.001:
            self.playback.set_sleep_gain(sleep_update.gain)
            self.sleep_gain = sleep_update.gain
        if sleep_update.pause:
            self.playback.sleep_at_position(sleep_update.rewind_to)
            save_playback_state(self.playback)
            self.last_playback_state_save = now
            self.menu.update_playback(self.playback.current_time, False)
            self.sleep_timer.confirm()
            self.screen.overlay = None
            self.screen.overlay_until = 0.0

    def update_listening_timer(self, session_id: str, now: float, elapsed: float) -> None:
        sleep_event = self.sleep_timer.update(
            now=now,
            elapsed_seconds=elapsed,
            enabled=self.config.sleep_timer_enabled and not self.bedtime.session_id,
            minutes=self.config.sleep_timer_minutes,
            session_id=session_id,
            is_playing=self.playback.is_playing,
        )
        apply_sleep_timer_event(
            sleep_event,
            self.sleep_timer,
            now,
            self.playback,
            self.menu,
            self.volume,
            self.muted,
            publisher=self.web_status,
        )
        if sleep_event == "prompt":
            self.screen.overlay = None
            self.screen.overlay_until = 0.0
            self.screen.last_input_at = now

    def publish_playback_transition(self) -> None:
        state = self.playback.state
        if state.is_playing != self.last_is_playing:
            self.last_is_playing = state.is_playing
            self.menu.update_playback(state.current_time, state.is_playing)
            self.web_status.publish(self.playback, self.volume, self.muted)

    def draw_frame(self, status: WifiStatus | None, now: float, brightness: int) -> None:
        muted = self.muted or self.volume <= 0
        if self.screen.draw_overlay(now, brightness, muted, self.bluetooth.connected):
            self.screen.opening_key = (
                self.playback.opening_key if self.screen.overlay == BOOK_LOADING_FRAME else ""
            )
        else:
            frame = self.screen.menu_frame(
                self.menu,
                self.catalogue,
                self.config,
                status,
                now,
                now - self.started_at < self.startup_network_grace_seconds,
            )
            self.screen.draw_menu(
                frame,
                self.menu,
                self.config,
                now,
                self.bedtime,
                self.sleep_timer,
                brightness,
                muted,
                self.bluetooth.connected,
            )
            if not isinstance(frame, SplashFrame) and not self.first_menu_drawn:
                self.first_menu_drawn = True
                self.gst_runtime.start_warmup()
                if not effective_muted(self.volume, self.muted):
                    play_boot_chime(min(self.volume, self.night_volume_limit))
        self.web_status.publish(self.playback, self.volume, self.muted, display_drawn=True)

    def close(self) -> None:
        with ExitStack() as cleanup:
            cleanup.callback(self.shelf.close)
            cleanup.callback(self.screen.display.close)
            if self.speakers is not None:
                cleanup.callback(self.speakers.shutdown)
            cleanup.callback(close_oled_playback, self.playback, self.saved_output_volumes)
            if self.amp_shutdown is not None:
                cleanup.callback(self.amp_shutdown.close)
            cleanup.callback(self.spoken_navigation.close)
            self.gst_runtime.stop_preloading()
            if self.speakers is not None:
                self.speakers.close()


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
