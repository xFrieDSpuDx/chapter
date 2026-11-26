from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from abs_kids_player.api import AudiobookshelfClient
from abs_kids_player.models import AudioTrack, Book, Chapter, PlaybackSession
from abs_kids_player.oled_service import handle_menu_command
from abs_kids_player.playback import GStreamerPlayback
from abs_kids_player.rotary_ui import ApplianceMenu, CommandType, Screen


def listening_session() -> PlaybackSession:
    return PlaybackSession(
        "session", "book", "Title", "Author", 240, 110, "",
        [AudioTrack(1, 0, 120, "first", "First"), AudioTrack(2, 120, 120, "second", "Second")],
        [Chapter(1, "One", 0, 120), Chapter(2, "Two", 120, 240)],
    )


class ListeningControlsTest(unittest.TestCase):
    def test_configured_skip_crosses_tracks_and_undo_returns_to_previous_track(self) -> None:
        session = listening_session()
        playback = GStreamerPlayback()
        playback.session = session
        playback.current_time = 110
        playback.Gst = SimpleNamespace(State=SimpleNamespace(PLAYING=1, PAUSED=2))
        playback.playbin = Mock()
        playback.load_current_track = Mock()
        menu = ApplianceMenu([Book("book", "Title", "Author", 240, "")], browsing=True)
        menu.set_session(session, is_playing=False)
        menu.skip_forward_seconds = 25
        menu.screen = Screen.LISTENING
        menu.listening_index = 2

        skip, = menu.click_nav()
        self.assertEqual(skip.seek_time, 135)
        handle_menu_command(skip, menu, playback)
        self.assertEqual((playback.track_index, playback.current_time), (1, 135))
        self.assertEqual(menu.undo_position, 110)

        menu.screen = Screen.LISTENING
        menu.listening_index = next(i for i, item in enumerate(menu.listening_options) if item[0] == "undo")
        undo, = menu.click_nav()
        handle_menu_command(undo, menu, playback)
        self.assertEqual((playback.track_index, playback.current_time), (0, 110))
        self.assertIsNone(menu.undo_position)
        self.assertEqual(playback.load_current_track.call_count, 2)
        playback.playbin.set_state.assert_called_with(playback.Gst.State.PAUSED)

    def test_chapter_picker_and_finish_confirmation_keep_existing_controls(self) -> None:
        menu = ApplianceMenu(browsing=True)
        menu.set_session(listening_session())
        menu.rotate_nav(2)
        self.assertEqual(menu.playing_action, "More")
        self.assertEqual(menu.spoken_selection().text, "More")
        menu.click_nav()
        menu.rotate_nav(3)
        menu.click_nav()
        menu.rotate_nav(1)
        command, = menu.click_nav()
        self.assertEqual((command.type, command.seek_time), (CommandType.SEEK_POSITION, 120))
        menu.click_nav()
        menu.listening_index = next(i for i, item in enumerate(menu.listening_options) if item[0] == "finish")
        menu.click_nav()
        self.assertEqual(menu.click_nav(), [])
        menu.click_nav()
        menu.listening_index = next(i for i, item in enumerate(menu.listening_options) if item[0] == "finish")
        menu.click_nav()
        menu.rotate_nav(1)
        command, = menu.click_nav()
        self.assertEqual(command.type, CommandType.MARK_FINISHED)

    def test_bookmark_sends_the_book_id_and_whole_second_position(self) -> None:
        client = AudiobookshelfClient("https://books.example.com", "token")
        with patch.object(client, "request") as request:
            client.create_bookmark("book", 23.98, "Chapter · 0:23")
        request.assert_called_once_with(
            "POST", "/api/me/item/book/bookmark", body={"time": 23, "title": "Chapter · 0:23"}
        )

    def test_failed_seek_does_not_save_the_requested_position(self) -> None:
        playback = GStreamerPlayback()
        playback.session = listening_session()
        playback.current_time = 110
        playback.Gst = Mock()
        playback.playbin = Mock()
        playback.seek_track = Mock(return_value=False)
        playback.request_progress_sync = Mock()
        self.assertFalse(playback.seek_global(25))
        self.assertEqual(playback.current_time, 110)
        playback.request_progress_sync.assert_not_called()

    def test_mark_finished_records_completion_before_stopping(self) -> None:
        playback = GStreamerPlayback()
        playback.session = listening_session()
        playback.Gst = Mock()
        playback.playbin = Mock()
        positions = []
        with patch.object(playback, "request_progress_sync", side_effect=lambda: positions.append(playback.current_time)):
            self.assertTrue(playback.mark_finished())
        self.assertEqual(positions, [240])
        self.assertIsNone(playback.session)
