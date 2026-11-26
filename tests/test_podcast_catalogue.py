from __future__ import annotations

import tempfile
import unittest
from unittest.mock import Mock, patch

from abs_kids_player.config import AppConfig, PodcastConfig
from abs_kids_player.models import Book
from abs_kids_player.podcast import (
    PodcastEpisode,
    PodcastProgressClient,
    latest_podcast_session,
    podcast_episode_id,
    rss_episode_books,
)
from abs_kids_player.podcast_catalogue import request_podcast_catalogue, server_podcast_episodes
from abs_kids_player.rotary_ui import ApplianceMenu, CommandType


class PodcastCatalogueTest(unittest.TestCase):
    def test_lookup_can_retry_if_the_worker_could_not_start(self):
        config = AppConfig()
        with patch("abs_kids_player.podcast_catalogue.Thread.start", side_effect=RuntimeError("worker unavailable")):
            first = request_podcast_catalogue(config)
        with self.assertRaisesRegex(RuntimeError, "worker unavailable"):
            first.result(timeout=1)
        self.assertEqual(request_podcast_catalogue(config).result(timeout=1), [])

    def test_rss_keeps_the_old_episode_position_after_a_new_episode_is_published(self) -> None:
        feed = PodcastConfig(
            "https://feeds.example.com/show", title="Show", feed_url="https://feeds.example.com/show", book_id="podcast:show"
        )
        first = PodcastEpisode("Old", "https://audio.example.com/old.mp3", 180, "old")
        newer = PodcastEpisode("New", "https://audio.example.com/new.mp3", 220, "new")
        episode_id = podcast_episode_id(first)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"ABS_KIDS_PLAYER_STATE_DIR": directory}):
            PodcastProgressClient("account", episode_id).update_progress(feed.book_id, 55, 180)
            with patch("abs_kids_player.podcast.read_podcast_episodes", return_value=[newer, first]):
                episodes = rss_episode_books(feed, "account")
                old_session = latest_podcast_session(feed, episode_id=episode_id, account="account")
                latest_session = latest_podcast_session(feed, account="account")

        self.assertEqual([(book.title, book.current_time) for book in episodes], [("New", 0), ("Old", 55)])
        self.assertEqual(old_session.tracks[0].url, first.audio_url)
        self.assertEqual(old_session.current_time, 55)
        self.assertEqual(latest_session.current_time, 0)

    def test_audiobookshelf_progress_matches_show_and_episode(self) -> None:
        client = Mock()
        client.get_libraries.return_value = [{"id": "books", "mediaType": "book"},
                                             {"id": "podcasts", "mediaType": "podcast"}]
        client.get_current_user.return_value = {"mediaProgress": [
            {"libraryItemId": "show", "episodeId": "old", "currentTime": 32},
            {"libraryItemId": "another-show", "episodeId": "new", "currentTime": 100},
        ]}
        client.request.return_value = {"results": [{
            "id": "show", "media": {
                "metadata": {"title": "Show"},
                "episodes": [
                    {"id": "old", "title": "Old", "duration": 100, "publishedAt": 1},
                    {"id": "new", "title": "New", "duration": 180, "publishedAt": 2},
                ],
            },
        }]}
        episodes = server_podcast_episodes(client)
        self.assertEqual([(book.episode_id, book.current_time) for book in episodes], [("new", 0), ("old", 32)])
        client.request.assert_called_once_with("GET", "/api/libraries/podcasts/items", query={"minified": 0})

    def test_knob_selects_the_requested_episode_by_show_and_episode_ids(self) -> None:
        menu = ApplianceMenu([Book("book", "A Book", "", 100, "")], browsing=True)
        menu.rotate_nav(-1)
        menu.click_nav()
        menu.rotate_nav(3)
        load, = menu.click_nav()
        self.assertEqual(load.type, CommandType.LOAD_PODCASTS)
        menu.set_books([*menu.books, Book(
            "show/old", "An older episode", "", 100, "", current_time=32,
            episode_id="old", source_item_id="show", podcast_title="Show",
        )])
        menu.click_nav()
        self.assertEqual(menu.render().bottom, "An older episode")
        menu.click_nav()
        play, = menu.click_nav()
        self.assertEqual((play.book_id, play.episode_id, play.resume_time), ("show", "old", 32))
