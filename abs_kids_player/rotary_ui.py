from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .bluetooth_picker import SpeakerPicker
from .bookmarks import Bookmark
from .config import DEFAULT_PREFER_DOWNLOADED, LIBRARY_SORT_TITLE, valid_library_sort_mode
from .display_frames import (
    MORE_ACTION_LABEL,
    BluetoothMenuFrame,
    ChapterConfirmFrame,
    DisplayFrame,
    HomeFrame,
    PlayingFrame,
    ResumeChoiceFrame,
    SleepConfirmFrame,
    StartupResumeFrame,
    TwoLineFrame,
    VolumeFrame,
)
from .library import book_section_letter, next_series_book
from .library_navigation import JUMP_TO_LETTER, LibraryBrowser
from .models import Book, Chapter, PlaybackSession
from .player_control import WebPlayerCommand
from .player_state import LastPlaybackState
from .volume_controls import stepped_volume_percent


class Screen(str, Enum):
    LIBRARY = "library"
    BROWSE = "browse"
    LISTENING = "listening"
    CHAPTER_PICKER = "chapter_picker"
    BOOKMARK_PICKER = "bookmark_picker"
    FINISH_CONFIRM = "finish_confirm"
    BEDTIME = "bedtime"
    STARTUP_RESUME = "startup_resume"
    RESUME_CHOICE = "resume_choice"
    PLAYING = "playing"
    CHAPTER_CONFIRM = "chapter_confirm"
    SLEEP_CONFIRM = "sleep_confirm"
    BLUETOOTH = "bluetooth"
    BLUETOOTH_DEVICES = "bluetooth_devices"


class CommandType(str, Enum):
    PLAY_BOOK = "play_book"
    RETRY_LIBRARY = "retry_library"
    PAUSE_PLAYBACK = "pause_playback"
    END_SELECTION = "end_selection"
    TOGGLE_PLAYBACK = "toggle_playback"
    SEEK_CHAPTER = "seek_chapter"
    SEEK_POSITION = "seek_position"
    UNDO_JUMP = "undo_jump"
    SAVE_BOOKMARK = "save_bookmark"
    LOAD_BOOKMARKS = "load_bookmarks"
    MARK_FINISHED = "mark_finished"
    SET_BEDTIME = "set_bedtime"
    LOAD_PODCASTS = "load_podcasts"
    HOME = "home"
    VOLUME_DELTA = "volume_delta"
    TOGGLE_MUTE = "toggle_mute"
    SHOW_IP = "show_ip"
    CLEAR_LAST_PLAYBACK = "clear_last_playback"
    SET_BLUETOOTH_ENABLED = "set_bluetooth_enabled"
    CONFIRM_STILL_LISTENING = "confirm_still_listening"


@dataclass(frozen=True)
class MenuCommand:
    type: CommandType
    book_id: str = ""
    start_over: bool = False
    resume_time: float = 0
    chapter_index: int = 0
    seek_time: float = 0
    volume_delta: int = 0
    display_seconds: int = 0
    bluetooth_enabled: bool = False
    selected_at: float | None = field(default=None, compare=False, repr=False)
    episode_id: str = ""
    timer_minutes: int = 0
    local_only: bool = False
    web_command: WebPlayerCommand | None = field(default=None, compare=False, repr=False)
    selection_key: str = ""


PLAYING_ACTIONS = ["Home", "Prev", "Play", "Next"]
PLAYING_PLAY_INDEX = 2
BEDTIME_OPTIONS = (("Back", None), ("15 minutes", 15), ("30 minutes", 30),
                   ("End of chapter", -1), ("Cancel timer", 0))
VOLUME_OVERLAY_SECONDS = 3
PLAYING_TITLE_WIDTH = 18
CHAPTER_CONFIRM_TIMEOUT_SECONDS = 5.0
HOME_RETURN_TIMEOUT_SECONDS = 5.0
SECTION_LETTER_HOLD_SECONDS = 1.0
SECTION_LETTER_FADE_SECONDS = 0.8
SECTION_LETTER_MAX_FILL = 255
POWER_OFF_RESUME_REWIND_SECONDS = 10.0
BOOK_TITLE_SPEECH_DELAY_SECONDS = 0.45


@dataclass(frozen=True)
class SpokenSelection:
    text: str
    delay_seconds: float = 0.0


class ApplianceMenu:
    def __init__(
        self,
        books: list[Book] | None = None,
        library_sort_mode: str = LIBRARY_SORT_TITLE,
        *,
        browsing: bool = False,
    ) -> None:
        self.books = books or []
        self.offline = False
        self.prefer_downloaded = DEFAULT_PREFER_DOWNLOADED
        self._index_root_books()
        self.browsing = browsing
        self.library_sort_mode = valid_library_sort_mode(library_sort_mode)
        self.browser = LibraryBrowser(self.library_sort_mode)
        self.browse_selected = False
        self.resume_from_browse = False
        self.listening_index = 0
        self.chapter_picker_index = 0
        self.bookmark_index = 0
        self.bookmarks: list[Bookmark] = []
        self.bookmark_status = "Loading bookmarks"
        self.bedtime_index = 0
        self.skip_back_seconds = 15
        self.skip_forward_seconds = 30
        self.undo_position: float | None = None
        self.selected_series = ""
        self.next_book: Book | None = None
        self.library_error: TwoLineFrame | None = None
        self.screen = Screen.LIBRARY
        self.book_index = self._root_indexes[0] if self._root_indexes else 0
        self.resume_choice_index = 0
        self.startup_resume_state: LastPlaybackState | None = None
        self.startup_resume_index = 0
        self.confirm_yes = True
        self.chapter_confirm_direction = "next"
        self.chapter_confirm_elapsed = 0.0
        self.home_return_elapsed = 0.0
        self.pending_chapter_index = 0
        self.sleep_confirm_seconds = 30
        self.bluetooth_enabled = True
        self.bluetooth_connected = False
        self.bluetooth_index = 2
        self.bluetooth_return_screen = Screen.LIBRARY
        self.speakers = SpeakerPicker()
        self.session: PlaybackSession | None = None
        self.session_key = ""
        self.current_time = 0.0
        self.is_playing = False
        self.playing_action_index = PLAYING_PLAY_INDEX
        self.volume_percent = 50
        self.volume_overlay_remaining = 0
        self.title_scroll_position = 0
        self._title_scroll_elapsed = 0.0
        self._title_scroll_subject = ""
        self.section_letter = ""
        self.section_letter_fill = 0
        self._section_letter_elapsed = 0.0

    def set_books(self, books: list[Book]) -> None:
        selected_id = self.books[self.book_index].id if self.books else None
        resume_open = self.screen == Screen.RESUME_CHOICE or (
            self.screen in (Screen.BLUETOOTH, Screen.BLUETOOTH_DEVICES)
            and self.bluetooth_return_screen == Screen.RESUME_CHOICE
        )
        choice = self.resume_options[self.resume_choice_index] if resume_open else None
        self.books = books
        self._index_root_books()
        self.browser.set_books(self._browse_books())
        if self.screen == Screen.LISTENING:
            self._find_next_book()
        self.library_error = None
        selected_index = next((index for index, book in enumerate(books) if book.id == selected_id), None)
        if selected_index is not None:
            self.book_index = selected_index
            if self.screen == Screen.LIBRARY and self._root_indexes and self.book_index not in self._root_positions:
                self.book_index = self._root_indexes[0]
            if resume_open:
                options = self.resume_options
                self.resume_choice_index = options.index(choice) if choice in options else 0
            return

        self.book_index = min(self.book_index, max(0, len(books) - 1))
        if self._root_indexes and self.book_index not in self._root_positions:
            self.book_index = self._root_indexes[0]
        self.clear_section_letter()
        if self.screen == Screen.RESUME_CHOICE or (not books and self.session is None and self.screen not in (Screen.BLUETOOTH, Screen.BLUETOOTH_DEVICES)):
            self.screen = Screen.BROWSE if self.resume_from_browse and books else Screen.LIBRARY
        if self.bluetooth_return_screen == Screen.RESUME_CHOICE:
            self.bluetooth_return_screen = Screen.LIBRARY
        if resume_open:
            self.resume_choice_index = 0

    def _index_root_books(self) -> None:
        visible = {book.id for book in self._browse_books() if self.offline or not book.download_only}
        self._root_indexes = [
            index for index, book in enumerate(self.books) if book.id in visible and not book.episode_id
        ]
        self._root_positions = {index: position for position, index in enumerate(self._root_indexes)}

    @property
    def browse_tile_selected(self) -> bool:
        return self.browse_selected or bool(self.browsing and self.books and not self._root_indexes)

    def _browse_books(self) -> list[Book]:
        if self.offline and (downloaded := [book for book in self.books if book.downloaded]):
            return downloaded
        return self.books

    def set_offline(self, offline: bool) -> None:
        if self.offline == offline:
            return
        self.offline = offline
        self.browser.offline = offline
        self._index_root_books()
        self.browser.set_books(self._browse_books())
        if self.offline and self.screen == Screen.STARTUP_RESUME and self.startup_resume_state:
            downloads = {book.id for book in self.books if book.downloaded}
            if downloads and self.startup_resume_state.book_id not in downloads:
                self.set_startup_resume(None)
                self.screen = Screen.LIBRARY
        if self.screen == Screen.LIBRARY and self._root_indexes and self.book_index not in self._root_positions:
            self.book_index = self._root_indexes[0]

    def _find_next_book(self) -> None:
        self.next_book = (
            next_series_book(self.books, self.session.library_item_id, self.selected_series)
            if self.session else None
        )

    def set_library_sort_mode(self, mode: str) -> None:
        self.library_sort_mode = valid_library_sort_mode(mode)
        self.browser.set_sort_mode(self.library_sort_mode)
        self.clear_section_letter()

    def reset_to_library(self) -> None:
        self.browser = LibraryBrowser(self.library_sort_mode, offline=self.offline)
        self.browse_selected = False
        self.resume_from_browse = False
        self.selected_series = ""
        self.session = None
        self.session_key = ""
        self.current_time = 0.0
        self.is_playing = False
        self.book_index = min(self.book_index, max(0, len(self.books) - 1))
        self.playing_action_index = PLAYING_PLAY_INDEX
        self.startup_resume_state = None
        self.startup_resume_index = 0
        self.resume_choice_index = 0
        self.pending_chapter_index = 0
        self.confirm_yes = True
        self.chapter_confirm_elapsed = 0.0
        self.home_return_elapsed = 0.0
        self.sleep_confirm_seconds = 30
        self.screen = Screen.LIBRARY
        self.reset_title_scroll()
        self.clear_section_letter()

    def set_session(self, session: PlaybackSession, is_playing: bool = True, *, session_key: str = "") -> None:
        if self.session is None or (self.session.id, self.session.library_item_id) != (session.id, session.library_item_id):
            self.undo_position = None
        self.session = session
        self.session_key = session_key
        self.current_time = session.current_time
        self.is_playing = is_playing
        self.playing_action_index = PLAYING_PLAY_INDEX
        self.reset_title_scroll()
        self.screen = Screen.PLAYING

    def set_startup_resume(self, state: LastPlaybackState | None) -> None:
        self.startup_resume_state = state if state and state.is_ready else None
        self.startup_resume_index = 0
        if self.startup_resume_state is not None:
            self.screen = Screen.STARTUP_RESUME

    def set_bookmarks(self, bookmarks: list[Bookmark] | None) -> None:
        self.bookmarks = bookmarks or []
        self.bookmark_index = 0
        self.bookmark_status = "Bookmarks unavailable" if bookmarks is None else "No saved bookmarks"

    def update_playback(self, current_time: float, is_playing: bool) -> None:
        self.current_time = max(0, current_time)
        self.is_playing = is_playing

    def note_input_activity(self) -> None:
        self.home_return_elapsed = 0.0

    def rotate_nav(self, steps: int) -> list[MenuCommand]:
        if steps == 0:
            return []
        direction = 1 if steps > 0 else -1
        for _ in range(abs(steps)):
            self.rotate_nav_once(direction)
        return []

    def rotate_nav_once(self, direction: int) -> None:
        if self.screen == Screen.LIBRARY:
            root = self._root_indexes
            current = self._root_positions.get(self.book_index, 0)
            index = len(root) if self.browse_selected else current
            count = len(root) + int(self.browsing and bool(self.books))
            index = wrap_index(index + direction, count)
            self.browse_selected = bool(self.books) and index == len(root)
            if not self.browse_selected and root:
                self.book_index = root[index]
            self.show_section_letter()
        elif self.screen == Screen.BROWSE:
            self.browser.move(direction)
        elif self.screen == Screen.STARTUP_RESUME:
            self.startup_resume_index = clamp(self.startup_resume_index + direction, 0, 1)
        elif self.screen == Screen.RESUME_CHOICE:
            self.resume_choice_index = clamp(
                self.resume_choice_index + direction,
                0,
                len(self.resume_options) - 1,
            )
        elif self.screen == Screen.PLAYING:
            self.playing_action_index = clamp(
                self.playing_action_index + direction,
                0,
                len(self.playing_actions) - 1,
            )
        elif self.screen == Screen.LISTENING:
            self.listening_index = wrap_index(self.listening_index + direction, len(self.listening_options))
        elif self.screen == Screen.CHAPTER_PICKER:
            self.chapter_picker_index = wrap_index(self.chapter_picker_index + direction, len(self.chapters) + 1)
        elif self.screen == Screen.BOOKMARK_PICKER:
            self.bookmark_index = wrap_index(self.bookmark_index + direction, len(self.bookmarks) + 1)
        elif self.screen == Screen.BEDTIME:
            self.bedtime_index = wrap_index(self.bedtime_index + direction, len(BEDTIME_OPTIONS))
        elif self.screen == Screen.FINISH_CONFIRM:
            self.confirm_yes = direction > 0
        elif self.screen == Screen.CHAPTER_CONFIRM:
            selected_index = 0 if self.confirm_yes else 1
            selected_index = clamp(selected_index + direction, 0, 1)
            self.confirm_yes = selected_index == 0
            self.chapter_confirm_elapsed = 0.0
        elif self.screen == Screen.BLUETOOTH:
            self.bluetooth_index = clamp(self.bluetooth_index + direction, 0, 2)
        elif self.screen == Screen.BLUETOOTH_DEVICES:
            self.speakers.rotate(direction)

    def click_nav(self) -> list[MenuCommand]:
        if self.screen == Screen.LIBRARY:
            if self.books and self.browse_tile_selected:
                self.browser.open(self._browse_books())
                self.screen = Screen.BROWSE
            elif self.books:
                self.resume_from_browse = False
                self.selected_series = ""
                self.resume_choice_index = self.default_resume_choice_index
                self.screen = Screen.RESUME_CHOICE
            elif self.library_error is not None:
                return [MenuCommand(CommandType.RETRY_LIBRARY)]
            return []

        if self.screen == Screen.BROWSE:
            loading_podcasts = self.browser.selected.key == "podcasts"
            choice = self.browser.choose()
            if loading_podcasts:
                return [MenuCommand(CommandType.LOAD_PODCASTS)]
            if not self.browser.pages:
                self.screen = Screen.LIBRARY
            if choice and choice.book:
                book = choice.book
                page = self.browser.pages[-1].key
                self.book_index = next(index for index, entry in enumerate(self.books) if entry.id == book.id)
                if page == JUMP_TO_LETTER:
                    self.browser.close()
                    self.browse_selected = False
                    self.resume_from_browse = False
                    self.selected_series = ""
                    self.screen = Screen.LIBRARY
                    self.show_section_letter()
                    return []
                self.selected_series = page.removeprefix("series/") if page.startswith("series/") else ""
                self.resume_from_browse = True
                self.resume_choice_index = self.default_resume_choice_index
                self.screen = Screen.RESUME_CHOICE
                if choice.play_now:
                    return [self._book_command(book, start_over=False)]
            return []

        if self.screen == Screen.STARTUP_RESUME:
            state = self.startup_resume_state
            self.startup_resume_state = None
            self.screen = Screen.LIBRARY
            if self.startup_resume_index == 1 or state is None:
                return [MenuCommand(CommandType.CLEAR_LAST_PLAYBACK)]
            return [
                MenuCommand(
                    CommandType.PLAY_BOOK,
                    book_id=state.book_id,
                    episode_id=state.episode_id,
                    start_over=False,
                    resume_time=power_off_resume_time(state.current_time),
                    local_only=self.offline,
                )
            ]

        if self.screen == Screen.RESUME_CHOICE:
            book = self.books[self.book_index]
            choice = self.resume_options[self.resume_choice_index]
            if choice == "Home":
                self.screen = Screen.BROWSE if self.resume_from_browse else Screen.LIBRARY
                return [MenuCommand(CommandType.HOME)]
            start_over = choice == "Start over"
            return [self._book_command(book, start_over)]

        if self.screen == Screen.PLAYING:
            action = self.playing_action
            if action == "Home":
                self.screen = Screen.LIBRARY
                return [MenuCommand(CommandType.HOME)]
            if action == "Prev":
                self.queue_chapter_jump(-1)
                return []
            if action == "Next":
                self.queue_chapter_jump(1)
                return []
            if action == "More":
                self.listening_index = 0
                self._find_next_book()
                self.screen = Screen.LISTENING
                return []
            return [MenuCommand(CommandType.TOGGLE_PLAYBACK)]

        if self.screen == Screen.LISTENING:
            return self._choose_listening_action()
        if self.screen == Screen.CHAPTER_PICKER:
            if not self.chapter_picker_index:
                self.screen = Screen.LISTENING
                return []
            self.screen = Screen.PLAYING
            return [MenuCommand(CommandType.SEEK_POSITION, seek_time=self.chapters[self.chapter_picker_index - 1].start)]
        if self.screen == Screen.BOOKMARK_PICKER:
            if self.bookmark_index == 0:
                self.screen = Screen.LISTENING
                return []
            self.screen = Screen.PLAYING
            return [MenuCommand(CommandType.SEEK_POSITION, seek_time=self.bookmarks[self.bookmark_index - 1].time)]
        if self.screen == Screen.FINISH_CONFIRM:
            self.screen = Screen.PLAYING
            return [MenuCommand(CommandType.MARK_FINISHED)] if self.confirm_yes else []
        if self.screen == Screen.BEDTIME:
            _, minutes = BEDTIME_OPTIONS[self.bedtime_index]
            self.screen = Screen.LISTENING if minutes is None else Screen.PLAYING
            return [] if minutes is None else [MenuCommand(CommandType.SET_BEDTIME, timer_minutes=minutes)]

        if self.screen == Screen.CHAPTER_CONFIRM:
            if not self.confirm_yes:
                self.screen = Screen.PLAYING
                return []
            chapter = self.chapters[self.pending_chapter_index]
            self.current_time = chapter.start
            self.screen = Screen.PLAYING
            return [
                MenuCommand(
                    CommandType.SEEK_CHAPTER,
                    chapter_index=self.pending_chapter_index,
                    seek_time=chapter.start,
                )
            ]

        if self.screen == Screen.SLEEP_CONFIRM:
            self.screen = Screen.PLAYING if self.session is not None else Screen.LIBRARY
            return [MenuCommand(CommandType.CONFIRM_STILL_LISTENING)]

        if self.screen == Screen.BLUETOOTH:
            option = self.bluetooth_options[self.bluetooth_index]
            if option == "Back":
                self.screen = self.bluetooth_return_screen
                return []
            if option in ("Enable", "Disable"):
                self.bluetooth_enabled = option == "Enable"
                return [
                    MenuCommand(
                        CommandType.SET_BLUETOOTH_ENABLED,
                        bluetooth_enabled=self.bluetooth_enabled,
                    )
                ]
            if option == "Speakers":
                self.screen = Screen.BLUETOOTH_DEVICES
                self.speakers.open()
                return []

        if self.screen == Screen.BLUETOOTH_DEVICES:
            if not self.speakers.select():
                self.screen = Screen.BLUETOOTH

        return []

    def rotate_volume(self, steps: int) -> list[MenuCommand]:
        if steps == 0:
            return []
        self.volume_percent = stepped_volume_percent(self.volume_percent, steps)
        self.volume_overlay_remaining = VOLUME_OVERLAY_SECONDS
        return [MenuCommand(CommandType.VOLUME_DELTA, volume_delta=steps)]

    def click_volume(self) -> list[MenuCommand]:
        return [MenuCommand(CommandType.TOGGLE_MUTE)]

    def hold_volume(self) -> list[MenuCommand]:
        return [MenuCommand(CommandType.SHOW_IP, display_seconds=5)]

    def hold_nav(self, bluetooth_enabled: bool = True, bluetooth_connected: bool = False) -> list[MenuCommand]:
        if self.screen == Screen.SLEEP_CONFIRM:
            return []
        if self.screen in (Screen.BLUETOOTH, Screen.BLUETOOTH_DEVICES):
            self.speakers.close()
            self.screen = Screen.LIBRARY
            return []
        self.open_bluetooth_menu(bluetooth_enabled, bluetooth_connected)
        return [MenuCommand(CommandType.PAUSE_PLAYBACK)]

    def open_bluetooth_menu(self, bluetooth_enabled: bool = True, bluetooth_connected: bool = False) -> None:
        if self.screen not in (Screen.BLUETOOTH, Screen.BLUETOOTH_DEVICES):
            self.bluetooth_return_screen = self.screen
        self.bluetooth_enabled = bluetooth_enabled
        self.bluetooth_connected = bluetooth_connected
        self.bluetooth_index = 2 if bluetooth_enabled else 1
        self.screen = Screen.BLUETOOTH

    def set_bluetooth_enabled(self, enabled: bool) -> None:
        self.bluetooth_enabled = enabled
        if not enabled:
            self.bluetooth_connected = False
        self.bluetooth_index = min(self.bluetooth_index, len(self.bluetooth_options) - 1)

    def set_bluetooth_connected(self, connected: bool) -> None:
        self.bluetooth_connected = connected
        self.bluetooth_index = min(self.bluetooth_index, len(self.bluetooth_options) - 1)

    def close_bluetooth_menu(self) -> None:
        self.speakers.close()
        self.screen = self.bluetooth_return_screen

    def render(self) -> DisplayFrame:
        if self.volume_overlay_remaining > 0:
            return self.render_volume()

        if self.screen == Screen.LIBRARY:
            if not self.books:
                return self.library_error or TwoLineFrame("No books", "Check library")
            if self.browse_tile_selected:
                return TwoLineFrame("Browse library", "Press to open")
            book = self.books[self.book_index]
            return HomeFrame(
                book.display_title,
                book.author,
                series_position=book.series_position,
                section_letter=self.section_letter,
                section_letter_fill=self.section_letter_fill,
                is_podcast=book_is_podcast(book),
            )

        if self.screen == Screen.BROWSE:
            return self.browser.render()
        if self.screen == Screen.LISTENING:
            return TwoLineFrame("More", self.selected_listening_option[1])
        if self.screen == Screen.CHAPTER_PICKER:
            title = self.chapters[self.chapter_picker_index - 1].title if self.chapter_picker_index else "Back"
            return TwoLineFrame("Choose chapter", title)
        if self.screen == Screen.BOOKMARK_PICKER:
            if self.bookmark_index:
                bookmark = self.bookmarks[self.bookmark_index - 1]
                return TwoLineFrame(f"Bookmark {format_time(bookmark.time)}", bookmark.title or "Saved position")
            return TwoLineFrame("Bookmarks" if self.bookmarks else self.bookmark_status, "Back")
        if self.screen == Screen.FINISH_CONFIRM:
            return TwoLineFrame("Mark this book finished?", "Yes" if self.confirm_yes else "No")
        if self.screen == Screen.BEDTIME:
            return TwoLineFrame("Sleep timer", BEDTIME_OPTIONS[self.bedtime_index][0])

        if self.screen == Screen.STARTUP_RESUME and self.startup_resume_state is not None:
            return StartupResumeFrame(
                self.startup_resume_state.title,
                selected_index=self.startup_resume_index,
            )

        if self.screen == Screen.RESUME_CHOICE:
            book = self.books[self.book_index]
            return ResumeChoiceFrame(book.display_title, self.resume_choice_index, self.resume_options)

        if self.screen == Screen.PLAYING and self.session:
            return PlayingFrame(
                chapter_text=self.playing_chapter_text,
                remaining_text=self.remaining_text,
                actions=self.playing_actions,
                selected_index=self.playing_action_index,
                title_scroll_px=self.title_scroll_position,
            )

        if self.screen == Screen.CHAPTER_CONFIRM and self.session:
            return ChapterConfirmFrame(
                direction=self.chapter_confirm_direction,
                selected_index=0 if self.confirm_yes else 1,
            )

        if self.screen == Screen.SLEEP_CONFIRM:
            return SleepConfirmFrame(self.sleep_confirm_seconds)

        if self.screen == Screen.BLUETOOTH:
            return BluetoothMenuFrame(
                enabled=self.bluetooth_enabled,
                selected_index=self.bluetooth_index,
                options=self.bluetooth_options,
            )

        if self.screen == Screen.BLUETOOTH_DEVICES:
            return self.speakers.frame()

        return TwoLineFrame("Loading", "")

    def spoken_selection(self) -> SpokenSelection | None:
        if self.screen == Screen.LIBRARY:
            if not self.books:
                return None
            if self.browse_tile_selected:
                return SpokenSelection("Browse library")
            return SpokenSelection(
                self.books[self.book_index].display_title,
                delay_seconds=BOOK_TITLE_SPEECH_DELAY_SECONDS,
            )
        if self.screen == Screen.BROWSE:
            return SpokenSelection(self.browser.selected.label, BOOK_TITLE_SPEECH_DELAY_SECONDS)
        if self.screen == Screen.LISTENING:
            return SpokenSelection(self.selected_listening_option[1])
        if self.screen in (Screen.CHAPTER_PICKER, Screen.BOOKMARK_PICKER, Screen.FINISH_CONFIRM, Screen.BEDTIME):
            return SpokenSelection(self.render().bottom)
        if self.screen == Screen.STARTUP_RESUME and self.startup_resume_state is not None:
            return SpokenSelection(("Continue", "Home")[self.startup_resume_index])
        if self.screen == Screen.RESUME_CHOICE:
            return SpokenSelection(self.resume_options[self.resume_choice_index])
        if self.screen == Screen.PLAYING and self.session is not None:
            return SpokenSelection(spoken_playing_action(self.playing_actions[self.playing_action_index]))
        if self.screen == Screen.CHAPTER_CONFIRM and self.session is not None:
            return SpokenSelection("Yes" if self.confirm_yes else "No")
        if self.screen == Screen.SLEEP_CONFIRM:
            return SpokenSelection("Yes")
        if self.screen == Screen.BLUETOOTH:
            return SpokenSelection(self.bluetooth_options[self.bluetooth_index])
        if self.screen == Screen.BLUETOOTH_DEVICES:
            return SpokenSelection(self.speakers.frame().device_name)
        return None

    @property
    def bluetooth_options(self) -> tuple[str, str, str]:
        return (
            "Back",
            "Disable" if self.bluetooth_enabled else "Enable",
            "Speakers",
        )

    def render_volume(self) -> VolumeFrame:
        return VolumeFrame(self.volume_percent, muted=self.volume_percent <= 0)

    def tick_overlay(self, seconds: int = 1) -> None:
        self.volume_overlay_remaining = max(0, self.volume_overlay_remaining - seconds)

    def tick_title_scroll(self, steps: int = 1) -> None:
        if self.screen != Screen.PLAYING or not self.session:
            return
        self.title_scroll_position = max(0, self.title_scroll_position + steps)

    def tick_chapter_confirm_timeout(
        self,
        elapsed_seconds: float,
        timeout_seconds: float = CHAPTER_CONFIRM_TIMEOUT_SECONDS,
    ) -> bool:
        if self.screen != Screen.CHAPTER_CONFIRM:
            self.chapter_confirm_elapsed = 0.0
            return False
        self.chapter_confirm_elapsed += max(0.0, elapsed_seconds)
        if self.chapter_confirm_elapsed < timeout_seconds:
            return False
        self.chapter_confirm_elapsed = 0.0
        self.screen = Screen.PLAYING
        return True

    def tick_home_return_timeout(
        self,
        elapsed_seconds: float,
        timeout_seconds: float = HOME_RETURN_TIMEOUT_SECONDS,
    ) -> bool:
        if self.screen != Screen.LIBRARY or self.session is None or not self.is_playing:
            self.home_return_elapsed = 0.0
            return False
        self.home_return_elapsed += max(0.0, elapsed_seconds)
        if self.home_return_elapsed < timeout_seconds:
            return False
        self.home_return_elapsed = 0.0
        self.playing_action_index = PLAYING_PLAY_INDEX
        self.reset_title_scroll()
        self.screen = Screen.PLAYING
        return True

    def show_sleep_confirmation(self, countdown_seconds: int = 30) -> None:
        self.sleep_confirm_seconds = max(0, countdown_seconds)
        self.playing_action_index = PLAYING_PLAY_INDEX
        self.screen = Screen.SLEEP_CONFIRM

    def update_sleep_confirmation(self, countdown_seconds: int) -> None:
        if self.screen == Screen.SLEEP_CONFIRM:
            self.sleep_confirm_seconds = max(0, countdown_seconds)

    def dismiss_sleep_confirmation(self) -> None:
        if self.screen == Screen.SLEEP_CONFIRM:
            self.screen = Screen.PLAYING if self.session is not None else Screen.LIBRARY

    def tick_section_letter(
        self,
        elapsed_seconds: float,
        hold_seconds: float = SECTION_LETTER_HOLD_SECONDS,
        fade_seconds: float = SECTION_LETTER_FADE_SECONDS,
    ) -> None:
        if not self.section_letter:
            return

        self._section_letter_elapsed += max(0.0, elapsed_seconds)
        if self._section_letter_elapsed <= hold_seconds:
            self.section_letter_fill = SECTION_LETTER_MAX_FILL
            return

        if fade_seconds <= 0:
            self.clear_section_letter()
            return

        fade_elapsed = self._section_letter_elapsed - hold_seconds
        if fade_elapsed >= fade_seconds:
            self.clear_section_letter()
            return

        progress = fade_elapsed / fade_seconds
        self.section_letter_fill = round(SECTION_LETTER_MAX_FILL * (1.0 - progress))

    def update_title_scroll(
        self,
        elapsed_seconds: float,
        delay_seconds: float,
        pixels_per_second: float,
    ) -> None:
        if self.screen != Screen.PLAYING or not self.session:
            self.reset_title_scroll()
            return

        chapter_text = self.playing_chapter_text
        if chapter_text != self._title_scroll_subject:
            self.reset_title_scroll(chapter_text)

        self._title_scroll_elapsed += max(0.0, elapsed_seconds)
        if self._title_scroll_elapsed < delay_seconds:
            self.title_scroll_position = 0
            return

        active_seconds = self._title_scroll_elapsed - delay_seconds
        self.title_scroll_position = max(0, round(active_seconds * pixels_per_second))

    def reset_title_scroll(self, subject: str = "") -> None:
        self.title_scroll_position = 0
        self._title_scroll_elapsed = 0.0
        self._title_scroll_subject = subject

    def show_section_letter(self) -> None:
        if not self.books or self.browse_tile_selected:
            self.clear_section_letter()
            return
        book = self.books[self.book_index]
        self.section_letter = book_section_letter(book, self.library_sort_mode)
        self.section_letter_fill = SECTION_LETTER_MAX_FILL
        self._section_letter_elapsed = 0.0

    def clear_section_letter(self) -> None:
        self.section_letter = ""
        self.section_letter_fill = 0
        self._section_letter_elapsed = 0.0

    def queue_chapter_jump(self, direction: int) -> None:
        if not self.session or not self.chapters:
            return
        current_index = self.current_chapter_index
        next_index = min(max(current_index + direction, 0), len(self.chapters) - 1)
        if next_index == current_index:
            return
        self.pending_chapter_index = next_index
        self.confirm_yes = True
        self.chapter_confirm_direction = "previous" if direction < 0 else "next"
        self.chapter_confirm_elapsed = 0.0
        self.screen = Screen.CHAPTER_CONFIRM

    @property
    def chapters(self) -> list[Chapter]:
        if not self.session:
            return []
        return self.session.chapters

    @property
    def current_chapter_index(self) -> int:
        for index, chapter in enumerate(self.chapters):
            if chapter.start <= self.current_time < chapter.end:
                return index
        return max(0, len(self.chapters) - 1)

    @property
    def current_chapter(self) -> Chapter:
        if not self.chapters:
            return Chapter(index=1, title="Chapter", start=0, end=0)
        return self.chapters[self.current_chapter_index]

    @property
    def playing_action(self) -> str:
        if self.playing_action_index == PLAYING_PLAY_INDEX:
            return "Play"
        action = self.playing_actions[self.playing_action_index]
        return "More" if action == MORE_ACTION_LABEL else action

    @property
    def action_row(self) -> str:
        actions = list(self.playing_actions)
        actions[self.playing_action_index] = f"[{actions[self.playing_action_index]}]"
        return " ".join(actions)

    @property
    def playing_actions(self) -> tuple[str, ...]:
        actions = PLAYING_ACTIONS.copy()
        actions[PLAYING_PLAY_INDEX] = "Pause" if self.is_playing else "Play"
        if self.browsing:
            actions.append(MORE_ACTION_LABEL)
        return tuple(actions)

    @property
    def listening_options(self) -> list[tuple[str, str]]:
        choices = [
            ("back", "Back"), ("skip_back", f"Back {self.skip_back_seconds}s"),
            ("skip_forward", f"Forward {self.skip_forward_seconds}s"), ("chapters", "Choose chapter"),
            ("bedtime", "Sleep timer"),
        ]
        if self.undo_position is not None:
            choices.append(("undo", "Undo last jump"))
        if self.session and not self.session.episode_id and not self.session.library_item_id.startswith("podcast:"):
            choices.extend((("bookmarks", "Bookmarks"), ("bookmark", "Save bookmark")))
        choices.extend((("stop", "Stop playback"), ("finish", "Mark finished")))
        if self.next_book:
            choices.append(("next_book", "Play next book"))
        return choices

    @property
    def selected_listening_option(self) -> tuple[str, str]:
        choices = self.listening_options
        return choices[min(self.listening_index, len(choices) - 1)]

    def _choose_listening_action(self) -> list[MenuCommand]:
        choice, _ = self.selected_listening_option
        if choice == "stop":
            return [MenuCommand(CommandType.END_SELECTION, selection_key=self.session_key)]
        if choice == "chapters":
            self.chapter_picker_index = self.current_chapter_index + 1 if self.chapters else 0
            self.screen = Screen.CHAPTER_PICKER
            return []
        if choice == "bookmarks":
            self.bookmarks = []
            self.bookmark_index = 0
            self.bookmark_status = "Loading bookmarks"
            self.screen = Screen.BOOKMARK_PICKER
            return [MenuCommand(CommandType.LOAD_BOOKMARKS)]
        if choice == "finish":
            self.confirm_yes = False
            self.screen = Screen.FINISH_CONFIRM
            return []
        if choice == "bedtime":
            self.bedtime_index = 0
            self.screen = Screen.BEDTIME
            return []
        self.screen = Screen.PLAYING
        if choice in ("skip_back", "skip_forward"):
            offset = -self.skip_back_seconds if choice == "skip_back" else self.skip_forward_seconds
            return [MenuCommand(CommandType.SEEK_POSITION, seek_time=self.current_time + offset)]
        if choice == "undo":
            return [MenuCommand(CommandType.UNDO_JUMP)]
        if choice == "bookmark":
            return [MenuCommand(CommandType.SAVE_BOOKMARK)]
        if choice == "next_book" and self.next_book:
            book = self.next_book
            return [self._book_command(book, start_over=False)]
        return []

    @property
    def playing_chapter_text(self) -> str:
        if not self.session:
            return ""
        return self.current_chapter.title

    @property
    def remaining_text(self) -> str:
        if not self.session:
            return ""
        remaining = max(0, self.session.duration - self.current_time)
        return f"-{format_time(remaining)}"

    @property
    def resume_options(self) -> tuple[str, ...]:
        if not self.books:
            return ("Home",)
        book = self.books[self.book_index]
        if book.has_play_history or book.finished:
            return ("Home", "Continue", "Start over")
        return ("Home", "Start")

    @property
    def default_resume_choice_index(self) -> int:
        return 1 if len(self.resume_options) > 1 else 0

    def _book_command(self, book: Book, start_over: bool) -> MenuCommand:
        downloaded_page = self.resume_from_browse and self.browser.pages and self.browser.pages[-1].key == "downloaded"
        return MenuCommand(
            CommandType.PLAY_BOOK,
            book_id=book.source_item_id or book.id,
            episode_id=book.episode_id,
            start_over=start_over,
            resume_time=0 if start_over else book.current_time,
            local_only=self.offline or bool(downloaded_page),
        )


def wrap_index(index: int, count: int) -> int:
    if count <= 0:
        return 0
    return index % count


def clamp(value: int, minimum: int, maximum: int) -> int:
    return min(max(value, minimum), maximum)


def spoken_playing_action(action: str) -> str:
    return {
        "Prev": "Previous chapter",
        "Next": "Next chapter",
        MORE_ACTION_LABEL: "More",
    }.get(action, action)


def book_is_podcast(book: Book) -> bool:
    return book.id.startswith("podcast:")


def power_off_resume_time(current_time: float) -> float:
    return max(0.0, current_time - POWER_OFF_RESUME_REWIND_SECONDS)


def marquee_text(text: str, width: int, position: int = 0, gap: int = 3) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    padded = text + " " * gap
    position = position % len(padded)
    doubled = padded + padded
    return doubled[position : position + width]


def marquee_cycle_length(text: str, width: int, gap: int = 3) -> int:
    if len(text) <= width:
        return 1
    return len(text) + gap


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
