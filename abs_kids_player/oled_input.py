"""Route rotary input to the OLED menu, volume control and spoken feedback."""

from __future__ import annotations

import queue
import threading
from dataclasses import replace
from typing import Callable

from .appliance_io import InputEvent
from .audio import DEFAULT_MIXER_CONTROLS, VolumeState
from .display_frames import DisplayFrame, VolumeFrame
from .network import setup_address_frame
from .rotary_ui import ApplianceMenu, CommandType, MenuCommand, SpokenSelection
from .volume_controls import VOLUME_INPUTS, apply_volume_input, effective_muted


NAVIGATION_INPUTS = frozenset({"nav", "nav_click", "nav_hold"})
SPOKEN_VOLUME_INTERVAL_PERCENT = 10
DEFAULT_WAKE_INPUT_QUIET_SECONDS = 0.35


class OledInputQueue(queue.SimpleQueue[InputEvent]):
    """Wake the display loop when a GPIO callback queues an input event."""

    def __init__(self) -> None:
        super().__init__()
        self._ready = threading.Event()
        self.last_dial_at: float | None = None
        self._wake_turn = ""

    def get_nowait(self) -> InputEvent:
        while True:
            event = super().get_nowait()
            previous = self.last_dial_at
            self.last_dial_at = event.received_at
            if (self._wake_turn == event.name and previous is not None
                    and event.received_at - previous < DEFAULT_WAKE_INPUT_QUIET_SECONDS):
                continue  # More detents from the same wake-up turn.
            self._wake_turn = ""
            return event

    def consume_panel_wake(self) -> bool:
        try:
            event = self.get_nowait()
        except queue.Empty:
            return False
        if event.name in ("nav", "volume"):
            self._wake_turn = event.name
        return True

    def put(self, item: InputEvent, block: bool = True, timeout: float | None = None) -> None:
        super().put(item, block, timeout)
        self._ready.set()

    def put_nowait(self, item: InputEvent) -> None:
        self.put(item)

    def wait_for_input(self, timeout: float) -> bool:
        self._ready.clear()
        return not self.empty() or self._ready.wait(timeout)


def handle_oled_input_events(
    events: queue.SimpleQueue[InputEvent],
    menu: ApplianceMenu,
    mixer_controls: tuple[str, ...] = DEFAULT_MIXER_CONTROLS,
    fallback_percent: int = 50,
    fallback_muted: bool = False,
    software_volume_only: bool = False,
    bluetooth_enabled: bool = True,
    bluetooth_connected: bool = False,
    control_click_feedback: Callable[[], None] | None = None,
    spoken_navigation_feedback: Callable[[SpokenSelection], None] | None = None,
    spoken_navigation_cancel: Callable[[], None] | None = None,
    opening_key: str = "",
    on_turn: Callable[[InputEvent], None] | None = None,
) -> tuple[DisplayFrame | None, int, bool, bool, list[MenuCommand], bool, bool]:
    frame: DisplayFrame | None = None
    volume = VolumeState(fallback_percent, fallback_muted)
    menu_changed = False
    commands: list[MenuCommand] = []
    pause_playback = False
    resume_playback = False

    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            return (
                frame,
                volume.percent,
                volume.muted,
                menu_changed,
                commands,
                pause_playback,
                resume_playback,
            )

        if on_turn is not None and event.name in ("nav", "volume"):
            on_turn(event)
        if event.name in NAVIGATION_INPUTS:
            if opening_key:
                if event.name == "nav_click":
                    commands.append(MenuCommand(CommandType.END_SELECTION, selection_key=opening_key))
                menu_changed = True
                continue
            commands.extend(
                _handle_navigation_input(
                    event,
                    menu,
                    bluetooth_enabled=bluetooth_enabled,
                    bluetooth_connected=bluetooth_connected,
                    control_click_feedback=control_click_feedback,
                    spoken_navigation_feedback=spoken_navigation_feedback,
                    spoken_navigation_cancel=spoken_navigation_cancel,
                )
            )
            menu_changed = True
        elif event.name in VOLUME_INPUTS:
            previous_volume = volume
            volume = apply_volume_input(
                event.name,
                event.steps,
                previous_volume,
                mixer_controls=mixer_controls,
                software_volume_only=software_volume_only,
            )
            frame = volume_frame_for_state(volume)
            was_muted = effective_muted(previous_volume.percent, previous_volume.muted)
            pause_playback |= not was_muted and frame.is_muted
            resume_playback |= was_muted and not frame.is_muted
            _speak_volume_state(
                frame,
                event.name,
                was_muted,
                previous_volume.percent,
                spoken_navigation_feedback,
            )
        elif event.name == "show_ip":
            frame = setup_address_frame()


def handle_input_events(
    events: queue.SimpleQueue[InputEvent],
    mixer_controls: tuple[str, ...] = DEFAULT_MIXER_CONTROLS,
) -> tuple[DisplayFrame | None, int, bool]:
    return handle_input_events_with_fallback(events, mixer_controls=mixer_controls)


def handle_input_events_with_fallback(
    events: queue.SimpleQueue[InputEvent],
    mixer_controls: tuple[str, ...] = DEFAULT_MIXER_CONTROLS,
    fallback_percent: int = 50,
    fallback_muted: bool = False,
) -> tuple[DisplayFrame | None, int, bool]:
    frame: DisplayFrame | None = None
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            return frame, fallback_percent, fallback_muted

        if event.name in VOLUME_INPUTS:
            state = apply_volume_input(
                event.name,
                event.steps,
                VolumeState(fallback_percent, fallback_muted),
                mixer_controls=mixer_controls,
            )
            fallback_percent = state.percent
            fallback_muted = state.muted
            frame = volume_frame_for_state(state)
        elif event.name == "show_ip":
            frame = setup_address_frame()


def volume_frame_for_state(state: VolumeState) -> VolumeFrame:
    return VolumeFrame(state.percent, muted=effective_muted(state.percent, state.muted))


def _handle_navigation_input(
    event: InputEvent,
    menu: ApplianceMenu,
    *,
    bluetooth_enabled: bool,
    bluetooth_connected: bool,
    control_click_feedback: Callable[[], None] | None,
    spoken_navigation_feedback: Callable[[SpokenSelection], None] | None,
    spoken_navigation_cancel: Callable[[], None] | None,
) -> list[MenuCommand]:
    if control_click_feedback is not None and (
        event.name == "nav_click" or (event.name == "nav" and event.steps)
    ):
        control_click_feedback()

    if event.name != "nav":
        print(f"Navigation {event.name.removeprefix('nav_')} received")
        if spoken_navigation_cancel is not None:
            spoken_navigation_cancel()

    previous_selection = menu.spoken_selection()
    commands = _update_menu(event, menu, bluetooth_enabled, bluetooth_connected)
    if spoken_navigation_feedback is not None:
        selection = menu.spoken_selection()
        if selection is not None and selection != previous_selection:
            spoken_navigation_feedback(selection)
    return commands


def _update_menu(
    event: InputEvent,
    menu: ApplianceMenu,
    bluetooth_enabled: bool,
    bluetooth_connected: bool,
) -> list[MenuCommand]:
    if event.name == "nav":
        menu.rotate_nav(event.steps)
        return []
    if event.name == "nav_click":
        return [
            replace(command, selected_at=event.received_at)
            if command.type == CommandType.PLAY_BOOK
            else command
            for command in menu.click_nav()
        ]
    return menu.hold_nav(
        bluetooth_enabled=bluetooth_enabled, bluetooth_connected=bluetooth_connected
    )


def _speak_volume_state(
    frame: VolumeFrame,
    event_name: str,
    was_muted: bool,
    previous_percent: int,
    feedback: Callable[[SpokenSelection], None] | None,
) -> None:
    if feedback is None:
        return
    if frame.is_muted:
        if not was_muted:
            feedback(SpokenSelection("Muted"))
    elif (
        was_muted
        or event_name == "volume_click"
        or (
            frame.percent != previous_percent
            and frame.percent % SPOKEN_VOLUME_INTERVAL_PERCENT == 0
        )
    ):
        feedback(SpokenSelection(f"Volume {frame.percent} percent"))


def input_activity_seen(
    overlay: DisplayFrame | None,
    menu_changed: bool,
    commands: list[MenuCommand],
    pause_playback: bool,
) -> bool:
    return overlay is not None or menu_changed or bool(commands) or pause_playback


def consume_screen_saver_wake_input(
    events: queue.SimpleQueue[InputEvent],
    screen_saver_visible: bool,
    now: float,
    quiet_until: float,
    quiet_seconds: float = DEFAULT_WAKE_INPUT_QUIET_SECONDS,
) -> tuple[bool, float]:
    if not screen_saver_visible and now >= quiet_until:
        return False, quiet_until

    consumed = drain_input_events(events)
    if consumed:
        quiet_until = now + quiet_seconds
    return screen_saver_visible and consumed, quiet_until


def drain_input_events(events: queue.SimpleQueue[InputEvent]) -> bool:
    consumed = False
    while True:
        try:
            events.get_nowait()
            consumed = True
        except queue.Empty:
            return consumed
