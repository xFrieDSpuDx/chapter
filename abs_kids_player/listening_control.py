"""Apply commands for the current session through the appliance's playback owner."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from typing import TYPE_CHECKING

from .catalogue_settings import catalogue_status_key
from .config import MAX_SLEEP_TIMER_MINUTES, PLAYBACK_SPEEDS, AppConfig, load_config
from .models import PlaybackSession
from .player_control import WebPlayerCommand, WebPlayerStatus, save_command_result
from .player_state import clear_last_playback


if TYPE_CHECKING:
    from .playback_owner import PlaybackOwner
    from .rotary_ui import ApplianceMenu


SESSION_ACTIONS = frozenset({
    "play", "pause", "volume", "mute", "unmute", "seek", "skip_back", "skip_forward",
    "speed", "undo", "sleep_timer", "bookmark", "finish", "stop",
})
SPEED_VALUES = frozenset(round(rate * 100) for rate in PLAYBACK_SPEEDS)
UNSCOPED_ACTIONS = frozenset({"play", "pause", "volume"})


def requires_active_session(command: WebPlayerCommand) -> bool:
    return bool(command.session_key or command.action in SESSION_ACTIONS - UNSCOPED_ACTIONS)


def control_value(action: str, raw: str, status: WebPlayerStatus) -> int | None:
    if action == "seek":
        value = int(raw)
        if status.duration > 0 and 0 <= value <= round(status.duration * 1000):
            return value
    elif action == "volume":
        value = int(raw)
        if 0 <= value <= 100:
            return value
    elif action == "speed":
        value = int(raw)
        if value in SPEED_VALUES:
            return value
    elif action == "sleep_timer":
        value = int(raw)
        if -1 <= value <= MAX_SLEEP_TIMER_MINUTES:
            return value
    elif action in SESSION_ACTIONS:
        return None
    raise ValueError("Invalid playback control")


def session_matches(playback: PlaybackOwner, command: WebPlayerCommand) -> bool:
    return bool(
        command.session_key and playback.state.session_key == command.session_key and not playback.loading_book
        and command.catalogue_key == catalogue_status_key(load_config())
    )


def seek_listening_position(
    playback: PlaybackOwner, menu: ApplianceMenu, position: float, *, undo: bool = False,
) -> bool:
    previous = playback.current_time
    if playback.seek_global(position) is False:
        return False
    menu.undo_position = None if undo else previous
    menu.update_playback(playback.current_time, playback.is_playing)
    return True


def update_menu_position(menu: ApplianceMenu, session: PlaybackSession, position: float | None) -> None:
    books = list(menu.books)
    for index, book in enumerate(books):
        if (book.source_item_id or book.id) != session.library_item_id or book.episode_id != session.episode_id:
            continue
        if position is None:
            books[index] = replace(book, is_finished=True)
        else:
            progress = position / book.duration if book.duration > 0 else 0
            books[index] = replace(book, current_time=position, progress=progress, is_finished=False)
    if books != menu.books:
        menu.set_books(books)


def finish_menu_book(menu: ApplianceMenu, session: PlaybackSession) -> None:
    update_menu_position(menu, session, None)
    clear_last_playback()
    menu.reset_to_library()


def end_book_selection(playback: PlaybackOwner, menu: ApplianceMenu, key: str) -> bool:
    """A Cancel click may arrive just as that same request becomes the playing session."""
    if not key or (playback.loading_book and playback.opening_key != key):
        return False
    starting = playback.starting_book
    cancelled = playback.cancel_opening(key)
    state = playback.state
    if state.session_key != key or state.session is None:
        if cancelled and (starting or state.session is None):
            menu.reset_to_library()
        return cancelled
    try:
        position = playback.stop_listening()
    finally:
        menu.reset_to_library()
    if position is None:
        return False
    update_menu_position(menu, state.session, position)
    return True


def apply_listening_control(
    command: WebPlayerCommand, playback: PlaybackOwner, menu: ApplianceMenu, config: AppConfig,
    arm_timer: Callable[[int], None],
) -> bool | Future[bool]:
    action, value = command.action, command.value
    if action == "seek" and value is not None:
        return seek_listening_position(playback, menu, value / 1000)
    if action in ("skip_back", "skip_forward"):
        offset = -config.skip_back_seconds if action == "skip_back" else config.skip_forward_seconds
        return seek_listening_position(playback, menu, playback.current_time + offset)
    if action == "undo" and menu.undo_position is not None:
        return seek_listening_position(playback, menu, menu.undo_position, undo=True)
    if action == "speed" and value in SPEED_VALUES:
        return playback.set_session_playback_rate(command.session_key, value / 100)
    if action == "sleep_timer" and value is not None:
        if value < 0 and menu.current_chapter.end <= playback.current_time:
            return False
        arm_timer(value)
        return True
    if action == "bookmark":
        return playback.save_bookmark()
    if action == "finish":
        session = playback.session
        if session is not None and playback.mark_finished():
            finish_menu_book(menu, session)
            return True
    return False


def record_control_outcome(command: WebPlayerCommand, result: bool | Future[bool]) -> None:
    if isinstance(result, Future):
        def finished(response: Future[bool]) -> None:
            applied = not response.cancelled() and response.exception() is None and response.result()
            save_command_result(command, "applied" if applied else "rejected")

        result.add_done_callback(finished)
    else:
        save_command_result(command, "applied" if result else "rejected")
