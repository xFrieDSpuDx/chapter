from __future__ import annotations

import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call, patch
from urllib.error import URLError

from abs_kids_player.api import AudiobookshelfClient, AudiobookshelfError
from abs_kids_player.config import AppConfig
from abs_kids_player.download_store import (
    book_directory,
    books_with_downloads,
    lock_download,
    read_download,
)
from abs_kids_player.download_transfer import DISK_RESERVE_BYTES, DownloadError
from abs_kids_player.download_views import render_library, search_books
from abs_kids_player.downloads import Downloads
from abs_kids_player.library import sorted_books_for_menu
from abs_kids_player.library_cache import save_cached_books
from abs_kids_player.models import Book
from abs_kids_player.playback import GStreamerPlayback
from abs_kids_player.playback_start import DownloadUnavailable, prepare_book_playback
from abs_kids_player.progress_sync import (
    PendingProgress,
    ProgressSync,
    progress_account,
    read_listening_progress,
)
from abs_kids_player.rotary_ui import ApplianceMenu, CommandType
from abs_kids_player.setup_server import SetupHandler
from abs_kids_player.setup_views import CSRF_FIELD_NAME


class AudioResponse(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200, range_header: str = "") -> None:
        super().__init__(data)
        self.status = status
        self.headers = {"ETag": '"track-version"'}
        if range_header:
            self.headers["Content-Range"] = range_header


class DownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch("abs_kids_player.download_transfer.save_download_cover"))
        self.enterContext(patch("abs_kids_player.downloads.DownloadAssetBackfill.schedule"))
        state_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict("os.environ", {
            "ABS_KIDS_PLAYER_STATE_PATH": str(state_dir / "state.json"),
            "ABS_KIDS_PLAYER_LIBRARY_CACHE_PATH": str(state_dir / "catalogue.json"),
        }))
        self.config = AppConfig(
            server_url="https://books.example.com", username="listener", token="token",
            library_id="library",
        )
        self.book = Book("book", "An Old Story", "Jane Example", 30, "", narrator="Sam Reader")
        self.enterContext(patch("abs_kids_player.api.urlopen", side_effect=URLError("offline")))
        self.directory = book_directory(self.config, self.book.id)
        self.client = Mock()
        self.client.request.return_value = {"media": {
            "tracks": [
                {"ino": "100", "duration": 10, "metadata": {"size": 4, "filename": "one.mp3"}},
                {"ino": "200", "duration": 20, "metadata": {"size": 6, "filename": "two.mp3"}},
            ],
            "chapters": [{"title": "First", "start": 0, "end": 10},
                         {"title": "Second", "start": 10, "end": 30}],
        }}
        self.client.open_download.side_effect = [AudioResponse(b"1234"), AudioResponse(b"567890")]
        self.workers = Downloads(config_loader=lambda: self.config, retry_seconds=0)

    def download(self) -> None:
        self.workers.start(self.config, self.book)
        with patch("abs_kids_player.download_transfer.client_from_config", return_value=self.client):
            self.assertTrue(self.workers.process_next())

    def test_preferred_copy_keeps_local_tracks_and_chapters(self) -> None:
        self.download()
        with (patch("abs_kids_player.playback_start.AudiobookshelfClient.start_playback") as stream,
              patch("abs_kids_player.api.urlopen", side_effect=TimeoutError) as unavailable):
            result = prepare_book_playback("book", self.config, resume_time=14)
        stream.assert_not_called()
        self.assertEqual(unavailable.call_count, 1)
        self.assertEqual(unavailable.call_args.args[0].full_url, "https://books.example.com/api/me/progress/book")
        self.assertEqual(unavailable.call_args.kwargs["timeout"], 1.0)
        session = result.session
        self.assertTrue(session.downloaded)
        self.assertEqual([track.start_offset for track in session.tracks], [0, 10])
        self.assertEqual([track.url for track in session.tracks], [
            (self.directory / "track-1").as_uri(), (self.directory / "track-2").as_uri(),
        ])
        self.assertEqual((session.chapters[1].title, session.current_time), ("Second", 14))
        self.assertEqual(result.client.progress_account, progress_account(self.config))
        self.assertEqual(session.id, "")

    def test_library_handoff_uses_browser_position_and_never_uploads_the_old_position(self) -> None:
        live_progress = self.enterContext(patch.object(AudiobookshelfClient, "get_progress"))
        self.download()
        account = progress_account(self.config)
        queue = ProgressSync()
        queue.record(account, "book", 4, 30)
        old = read_listening_progress()[(account, "book", "")]
        server = Mock()
        server.get_progress.return_value = {}
        with patch("abs_kids_player.api.client_from_config", return_value=server):
            with patch("abs_kids_player.progress_sync.threading.Thread") as worker:
                queue.pump(self.config)
                task = worker.call_args.kwargs
                task["target"](*task["args"])
        server.update_progress.reset_mock()
        live_progress.return_value = {"currentTime": 18, "lastUpdate": old.updated_at + 1000}
        for local_only in (False, True):
            session = prepare_book_playback("book", self.config, resume_time=4, local_only=local_only).session
            self.assertTrue(session.downloaded)
            self.assertEqual((session.current_time, session.progress_updated_at), (18, old.updated_at + 1000))
        playback = GStreamerPlayback(progress_sync=queue)
        playback.session, playback.client = session, server
        server.progress_account = account
        playback.current_time = 20
        playback.request_progress_sync()
        server.get_progress.return_value = live_progress.return_value
        with patch("abs_kids_player.api.client_from_config", return_value=server):
            with patch("abs_kids_player.progress_sync.threading.Thread") as worker:
                queue.pump(self.config)
                task = worker.call_args.kwargs
                task["target"](*task["args"])
        self.assertEqual(server.update_progress.call_args.args, ("book", 20, 30))

    def test_browser_rewind_to_zero_and_newer_offline_edits_use_edit_order(self) -> None:
        live_progress = self.enterContext(patch.object(AudiobookshelfClient, "get_progress"))
        self.download()
        queue = ProgressSync()
        key = progress_account(self.config), "book", ""
        queue.record(key[0], "book", 6, 30)
        edited = read_listening_progress()[key].updated_at
        for remote_edit, expected in ((edited - 1, 6), (edited + 1, 0)):
            live_progress.return_value = {"currentTime": 0, "lastUpdate": remote_edit}
            session = prepare_book_playback("book", self.config, resume_time=14).session
            self.assertEqual(session.current_time, expected)
        live_progress.side_effect = AudiobookshelfError("offline")
        session = prepare_book_playback("book", self.config).session
        self.assertEqual(session.current_time, 6)

    def test_finished_download_replays_only_if_it_is_still_finished_on_the_server(self) -> None:
        self.book = replace(self.book, current_time=30, is_finished=True, progress_updated_at=100)
        self.download()
        for remote, expected in (({"currentTime": 8, "isFinished": False}, 8),
                                 ({"currentTime": 30, "isFinished": True}, 0)):
            with patch.object(AudiobookshelfClient, "get_progress", return_value={**remote, "lastUpdate": 200}):
                self.assertEqual(prepare_book_playback("book", self.config).session.current_time, expected)

    def test_disabled_preference_streams_but_download_view_and_network_fallback_play_locally(self) -> None:
        self.download()
        config = replace(self.config, prefer_downloaded=False)
        with patch("abs_kids_player.playback_start.AudiobookshelfClient.start_playback", return_value=Mock()) as stream:
            self.assertIs(prepare_book_playback("book", config).session, stream.return_value)
            self.assertTrue(prepare_book_playback("book", config, local_only=True).session.downloaded)
            stream.assert_called_once()
            stream.side_effect = AudiobookshelfError("offline")
            self.assertTrue(prepare_book_playback("book", config).session.downloaded)

    def test_default_streams_when_a_local_track_is_incomplete_or_the_book_is_absent(self) -> None:
        self.download()
        (self.directory / "track-1").write_bytes(b"12")
        with patch("abs_kids_player.playback_start.AudiobookshelfClient.start_playback") as stream:
            for item_id in ("book", "other"):
                with self.subTest(item_id=item_id):
                    result = prepare_book_playback(item_id, self.config)
                    self.assertIs(result.session, stream.return_value)
                    stream.assert_called_with(item_id, start_over=False, resume_time=0)

    def test_start_over_skips_saved_positions_for_every_download_entry_point(self) -> None:
        self.download()
        save_cached_books(self.config, [replace(self.book, current_time=14, progress_updated_at=1000)])
        with (
            patch("abs_kids_player.library_cache.replace", side_effect=AssertionError("copied catalogue row")),
            patch("abs_kids_player.download_store.read_listening_progress", side_effect=AssertionError("read local positions")),
            patch("abs_kids_player.playback_start.read_pending_progress", side_effect=AssertionError("read pending positions")),
            patch("abs_kids_player.playback_start.AudiobookshelfClient.start_playback", side_effect=AudiobookshelfError("offline")) as stream,
        ):
            for prefer, local_only in ((True, False), (False, True), (False, False)):
                with self.subTest(prefer=prefer, local_only=local_only):
                    session = prepare_book_playback(
                        "book", replace(self.config, prefer_downloaded=prefer), resume_time=20,
                        start_over=True, local_only=local_only,
                    ).session
                    self.assertTrue(session.downloaded)
                    self.assertEqual((session.current_time, session.progress_updated_at), (0, 1000))
            stream.assert_called_once()

    def test_continue_uses_newest_progress_including_a_rewind_to_zero(self) -> None:
        self.book = replace(self.book, current_time=5, progress_updated_at=100)
        self.download()
        account = progress_account(self.config)
        # Positions intentionally move backwards as their timestamps advance.
        for catalogue_time, local_time, expected in (
            (80, 90, (5, 100)), (200, 150, (3, 200)), (200, 210, (0, 210)), (200, 200, (0, 200)),
        ):
            with self.subTest(catalogue=catalogue_time, local=local_time):
                save_cached_books(self.config, [replace(self.book, current_time=3, progress_updated_at=catalogue_time)])
                local = PendingProgress(account, self.book.id, 0, self.book.duration, local_time)
                with (
                    patch("abs_kids_player.library_cache.replace", side_effect=AssertionError("copied catalogue row")),
                    patch("abs_kids_player.download_store.read_listening_progress", return_value={local.key: local}),
                ):
                    session = prepare_book_playback("book", self.config, resume_time=20).session
                self.assertEqual((session.current_time, session.progress_updated_at), expected)

    def test_download_validation_stats_each_track_once_and_rejects_non_files(self) -> None:
        self.download()
        track = self.directory / "track-2"
        other = self.directory / "track-1"
        with patch.object(Path, "stat", autospec=True, side_effect=Path.stat) as stat:
            self.assertTrue(read_download(self.directory).complete)
        self.assertEqual([call.args[0] for call in stat.call_args_list], [other, track])
        track.write_bytes(b"x")
        self.assertFalse(read_download(self.directory).complete)
        track.unlink()
        self.assertFalse(read_download(self.directory).complete)
        track.mkdir()
        self.assertFalse(read_download(self.directory).complete)

    def test_local_start_and_continue_ignore_progress_from_another_library_cache(self) -> None:
        self.book = replace(self.book, current_time=11, progress_updated_at=50)
        self.download()
        save_cached_books(replace(self.config, library_id="other"), [
            replace(self.book, current_time=23, progress_updated_at=100),
        ])
        for restart, expected in ((True, 0), (False, 11)):
            with self.subTest(restart=restart):
                session = prepare_book_playback("book", self.config, start_over=restart).session
                self.assertEqual((session.current_time, session.progress_updated_at), (expected, 50))

    def test_interruption_keeps_partial_hidden_and_retry_requests_remaining_bytes(self) -> None:
        self.client.open_download.side_effect = [AudioResponse(b"1234"), AudioResponse(b"56")]
        self.download()
        self.assertFalse(read_download(self.directory).complete)
        self.assertEqual(books_with_downloads(self.config, [self.book]), [self.book])
        self.client.open_download.reset_mock()
        self.client.open_download.side_effect = [AudioResponse(b"7890", 206, "bytes 2-5/6")]
        self.download()
        self.assertEqual(self.client.open_download.call_args.args[:2], ("/api/items/book/file/200/download", 2))
        self.assertEqual(self.client.open_download.call_args.kwargs["validator"], '"track-version"')
        self.assertEqual((self.directory / "track-2").read_bytes(), b"567890")
        self.assertTrue(read_download(self.directory).complete)

    def test_low_storage_refuses_before_audio_transfer(self) -> None:
        with patch("abs_kids_player.download_transfer.storage_usage", return_value=(0, DISK_RESERVE_BYTES)):
            self.download()
        self.client.open_download.assert_not_called()
        self.assertIn("insufficient", self.workers.status(self.config, "book")[0])
        self.assertFalse(self.directory.exists())

    def test_server_replacing_a_file_restarts_partial_instead_of_appending(self) -> None:
        self.client.open_download.side_effect = [AudioResponse(b"12")]
        self.download()
        self.client.open_download.side_effect = [AudioResponse(b"abcd"), AudioResponse(b"123456")]
        self.download()
        self.assertEqual((self.directory / "track-1").read_bytes(), b"abcd")
        self.assertTrue(read_download(self.directory).complete)

    def test_cancelled_or_removed_books_are_unavailable_offline(self) -> None:
        self.workers.start(self.config, self.book)
        self.workers.cancel(self.config, self.book.id)
        self.assertIn("Cancelled", self.workers.status(self.config, "book")[0])
        self.download()
        with lock_download(self.directory, shared=True), self.assertRaisesRegex(DownloadError, "open in the player"):
            self.workers.remove(self.config, "book")
        self.workers.remove(self.config, "book")
        with self.assertRaises(DownloadUnavailable):
            prepare_book_playback("book", self.config, local_only=True)

    def test_offline_boot_browses_only_complete_downloads_for_the_current_account(self) -> None:
        self.download()
        self.assertFalse(books_with_downloads(replace(self.config, username="another"), []))
        other = Book("remote", "A Remote Book", "", 30, "")
        menu = ApplianceMenu(books_with_downloads(self.config, [other]), browsing=True)
        menu.set_offline(True)
        self.assertEqual(menu.render().top, "An Old Story")
        menu.click_nav()
        command, = menu.click_nav()
        self.assertEqual((command.type, command.book_id, command.local_only), (CommandType.PLAY_BOOK, "book", True))

    def test_previous_library_downloads_stay_in_downloaded_after_a_library_switch(self) -> None:
        self.download()
        config = replace(self.config, library_id="new-library")
        new_book = Book("z", "Zebra Stories", "", 60, "")
        menu = ApplianceMenu(sorted_books_for_menu(books_with_downloads(config, [new_book])), browsing=True)
        self.assertEqual(menu.render().top, "Zebra Stories")
        menu.rotate_nav(-1)
        menu.click_nav()
        menu.rotate_nav(-3)
        self.assertEqual(menu.render().bottom, "Downloaded")
        menu.click_nav()
        self.assertEqual(menu.render().bottom, "An Old Story")

    def test_downloaded_book_stopped_offline_syncs_latest_rewind_after_restart(self) -> None:
        self.download()
        sync = ProgressSync(retry_seconds=0)
        playback = GStreamerPlayback(progress_sync=sync)
        prepared = prepare_book_playback("book", self.config, local_only=True)
        client = prepared.client
        key = (progress_account(self.config), "book", "")

        def pump_once(queue):
            # Run this worker to completion before simulating the next network state.
            with patch("abs_kids_player.progress_sync.threading.Thread") as thread:
                queue.pump(self.config)
                task = thread.call_args.kwargs
                task["target"](*task["args"])

        with (
            patch("abs_kids_player.api.client_from_config", return_value=client),
            patch.object(client, "request", side_effect=AudiobookshelfError("offline")) as request,
        ):
            playback.session, playback.client = prepared.session, client
            playback.current_time = 22
            self.assertEqual(playback.stop_listening(), 22)
            pump_once(sync)
            self.assertFalse(read_listening_progress()[key].synced)

            playback.session, playback.client = prepared.session, client
            playback.current_time = 0
            self.assertEqual(playback.stop_listening(), 0)
            self.assertEqual(prepare_book_playback("book", self.config, resume_time=22).session.current_time, 0)

            request.reset_mock()
            request.side_effect = [{"currentTime": 22, "lastUpdate": 1}, None]
            pump_once(ProgressSync())

        self.assertEqual(request.call_args_list, [
            call("GET", "/api/me/progress/book"),
            call("PATCH", "/api/me/progress/book", body={
                "currentTime": 0, "duration": 30, "progress": 0.0, "isFinished": False,
            }),
        ])
        self.assertTrue(read_listening_progress()[key].synced)
        self.assertEqual(prepare_book_playback("book", self.config, resume_time=22).session.current_time, 0)

    def test_search_and_downloaded_filter_match_narrator_and_exclude_partials(self) -> None:
        self.download()
        books = books_with_downloads(self.config, [Book("other", "One More Story", "", 30, "", narrator="Sam Reader")])
        self.assertEqual([book.id for book in search_books(books, "Old Reader", True)], ["book"])
        page = render_library(self.config, books, "Sam Reader", True, 1, "token")
        self.assertNotIn("Prefer downloaded books", page)
        self.assertIn("icon-trash", page)
        self.assertIn("An Old Story", page)
        self.assertNotIn("One More Story", page)

    def test_library_route_saves_preferences_with_a_csrf_protected_post(self) -> None:
        handler = object.__new__(SetupHandler)
        handler.path = "/library/settings"
        handler.csrf_token = Mock(return_value="csrf-token")
        handler.post_fields = Mock(return_value={
            CSRF_FIELD_NAME: ["csrf-token"], "prefer_downloaded": ["1"], "download_limit_mib": ["1024"],
        })
        handler.send_csrf_error = Mock()
        handler.redirect_library = Mock()
        from abs_kids_player.config import load_config, save_config

        with patch.dict("os.environ", {"ABS_KIDS_PLAYER_CONFIG_PATH": str(self.directory.parent / "settings.json")}):
            save_config(replace(self.config, prefer_downloaded=False))
            handler.do_POST()
            saved = load_config()
        handler.send_csrf_error.assert_not_called()
        self.assertTrue(saved.prefer_downloaded)
        self.assertEqual(saved.download_limit_mib, 1024)
