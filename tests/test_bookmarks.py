import unittest
from concurrent.futures import Future
from dataclasses import replace
from unittest.mock import patch

from abs_kids_player.api import AudiobookshelfError
from abs_kids_player.bookmarks import Bookmark, read_bookmarks, request_bookmarks
from abs_kids_player.config import AppConfig
from abs_kids_player.models import PlaybackSession
from abs_kids_player.oled_bookmarks import OledBookmarkPicker
from abs_kids_player.progress_sync import progress_account
from abs_kids_player.rotary_ui import ApplianceMenu, CommandType, Screen


class BookmarksTest(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig(server_url="https://books.example", username="listener", token="token", library_id="main")
        self.session = PlaybackSession("", "book", "Story", "", 1200, 20, "", [], [],
                                       progress_account=progress_account(self.config))

    @patch("abs_kids_player.bookmarks.client_from_config")
    def test_loads_only_the_current_books_valid_bookmarks_in_time_order(self, client):
        client.return_value.get_current_user.return_value = {"bookmarks": [
            {"libraryItemId": "book", "time": 500.5, "title": "Late scene"},
            {"libraryItemId": "another", "time": 30, "title": "Other book"},
            {"libraryItemId": "book", "time": 0, "title": " Opening "},
            {"libraryItemId": "book", "time": float("inf"), "title": "Invalid"},
            {"libraryItemId": "book", "time": 1200, "title": "End of book"},
        ]}
        self.assertEqual(read_bookmarks(self.config, self.session), [Bookmark(0, "Opening"), Bookmark(500.5, "Late scene")])
        client.assert_called_once_with(self.config)

    @patch("abs_kids_player.bookmarks.client_from_config")
    def test_never_loads_bookmarks_for_previous_account_and_worker_reports_offline(self, client):
        self.assertIsNone(request_bookmarks(replace(self.config, username="other"), self.session).result(timeout=2))
        client.assert_not_called()
        client.return_value.get_current_user.side_effect = AudiobookshelfError("Offline")
        self.assertIsNone(request_bookmarks(self.config, self.session).result(timeout=2))

    def test_more_bookmarks_opens_picker_and_seeks_to_selected_position(self):
        menu = ApplianceMenu(browsing=True)
        menu.set_session(self.session)
        menu.screen = Screen.LISTENING
        menu.listening_index = next(i for i, (key, _) in enumerate(menu.listening_options) if key == "bookmarks")
        queued, = menu.click_nav()
        self.assertEqual(queued.type, CommandType.LOAD_BOOKMARKS)
        self.assertEqual(menu.render().top, "Loading bookmarks")
        menu.set_bookmarks([Bookmark(83, "Favourite scene")])
        menu.rotate_nav(1)
        self.assertEqual((menu.render().top, menu.render().bottom), ("Bookmark 1:23", "Favourite scene"))
        jump, = menu.click_nav()
        self.assertEqual((jump.type, jump.seek_time, menu.screen), (CommandType.SEEK_POSITION, 83, Screen.PLAYING))

    def test_empty_and_unavailable_picker_have_a_way_back(self):
        menu = ApplianceMenu()
        for bookmarks, title in (([], "No saved bookmarks"), (None, "Bookmarks unavailable")):
            menu.screen = Screen.BOOKMARK_PICKER
            menu.set_bookmarks(bookmarks)
            self.assertEqual((menu.render().top, menu.render().bottom), (title, "Back"))
            self.assertEqual(menu.click_nav(), [])
            self.assertEqual(menu.screen, Screen.LISTENING)

    def open_picker(self, picker, menu):
        menu.screen = Screen.LISTENING
        menu.listening_index = next(i for i, (key, _) in enumerate(menu.listening_options) if key == "bookmarks")
        command, = menu.click_nav()
        self.assertEqual(command.type, CommandType.LOAD_BOOKMARKS)
        self.assertTrue(picker.open(self.config, menu))

    @patch("abs_kids_player.oled_bookmarks.request_bookmarks")
    def test_leave_then_reopen_while_the_first_lookup_is_still_running(self, request):
        old, new = Future(), Future()
        request.side_effect = [old, new]
        menu = ApplianceMenu(browsing=True)
        menu.set_session(self.session)
        picker = OledBookmarkPicker()
        self.open_picker(picker, menu)
        self.assertFalse(picker.apply_ready(self.config, menu))
        menu.click_nav()  # Back is available during the lookup.
        self.assertEqual(menu.screen, Screen.LISTENING)
        self.assertFalse(picker.apply_ready(self.config, menu))
        self.open_picker(picker, menu)
        new.set_result([Bookmark(41, "Current")])
        self.assertTrue(picker.apply_ready(self.config, menu))
        old.set_result([Bookmark(2, "Stale")])
        self.assertFalse(picker.apply_ready(self.config, menu))
        self.assertEqual(menu.bookmarks, [Bookmark(41, "Current")])

    @patch("abs_kids_player.oled_bookmarks.request_bookmarks")
    def test_result_arriving_after_back_leaves_the_listening_menu_alone(self, request):
        menu = ApplianceMenu(browsing=True)
        menu.set_session(self.session)
        picker = OledBookmarkPicker()
        pending = request.return_value = Future()
        self.open_picker(picker, menu)
        menu.click_nav()
        pending.set_result([Bookmark(4, "Left behind")])
        self.assertFalse(picker.apply_ready(self.config, menu))
        self.assertEqual(menu.screen, Screen.LISTENING)
        self.assertEqual(menu.bookmarks, [])

    @patch("abs_kids_player.oled_bookmarks.request_bookmarks")
    def test_switch_session_or_account_during_lookup(self, request):
        menu = ApplianceMenu(browsing=True)
        menu.set_session(self.session)
        picker = OledBookmarkPicker()
        pending = request.return_value = Future()
        self.open_picker(picker, menu)
        menu.set_session(replace(self.session, library_item_id="another"))
        menu.screen = Screen.BOOKMARK_PICKER
        pending.set_result([Bookmark(3, "Previous book")])
        self.assertFalse(picker.apply_ready(self.config, menu))
        self.assertEqual(menu.bookmarks, [])

        pending = request.return_value = Future()
        self.open_picker(picker, menu)
        other = replace(self.config, username="other")
        self.assertTrue(picker.apply_ready(other, menu))
        self.assertEqual(menu.render().top, "Bookmarks unavailable")
        pending.set_result([Bookmark(4, "Previous account")])
        self.assertFalse(picker.apply_ready(self.config, menu))
        self.assertEqual(menu.bookmarks, [])
