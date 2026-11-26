from __future__ import annotations

import io
import unittest
from email.message import Message
from unittest.mock import Mock, patch

from abs_kids_player.config import AppConfig
from abs_kids_player.player_control import WebPlayerStatus
from abs_kids_player.setup_views import render_player_status_value
from abs_kids_player.web_artwork import MAX_COVER_BYTES, artwork_url, fetch_artwork
from abs_kids_player.web_player_events import player_status_event_signature, player_status_payload


def response(content_type, body):
    stream = io.BytesIO(body)
    stream.headers = Message()
    stream.headers["Content-Type"] = content_type
    return stream


class WebArtworkTest(unittest.TestCase):
    def test_cover_uses_the_authenticated_thumbnail_endpoint(self):
        config = AppConfig(server_url="https://books.example.test", token="private", username="a")
        client = Mock()
        client.open_download.return_value = response("image/webp", b"cover")
        with patch("abs_kids_player.web_artwork.client_from_config", return_value=client):
            self.assertEqual(fetch_artwork(config, "book-1"), ("image/webp", b"cover"))
            self.assertEqual(fetch_artwork(config, "book-1"), ("image/webp", b"cover"))
        client.open_download.assert_called_once_with("/api/items/book-1/cover?width=320&format=webp", timeout=2)

    def test_other_accounts_cannot_reuse_cached_thumbnails(self):
        client = Mock()
        client.open_download.side_effect = [response("image/jpeg", b"a"), OSError()]
        with patch("abs_kids_player.web_artwork.client_from_config", return_value=client):
            for username, result in (("a", ("image/jpeg", b"a")), ("b", None)):
                config = AppConfig(server_url="https://account-cover.example.test", token="private", username=username)
                self.assertEqual(fetch_artwork(config, "same-item"), result)

    def test_artwork_rejects_full_urls_unexpected_media_and_oversized_responses(self):
        config = AppConfig(server_url="https://bounds.example.test", token="private")
        client = Mock()
        client.open_download.side_effect = [
            response("image/svg+xml", b"<svg onload='unexpected'/>"),
            response("image/webp", b"x" * (MAX_COVER_BYTES + 1)),
        ]
        with patch("abs_kids_player.web_artwork.client_from_config", return_value=client):
            self.assertIsNone(fetch_artwork(config, "http://unsafe/image"))
            self.assertIsNone(fetch_artwork(config, "svg"))
            self.assertIsNone(fetch_artwork(config, "large"))
        self.assertEqual(client.open_download.call_count, 2)
        self.assertEqual(artwork_url("../settings"), "")

    def test_unavailable_artwork_does_not_retry_on_every_poll(self):
        config = AppConfig(server_url="https://repeat-cover.example.test", token="private")
        client = Mock()
        client.open_download.side_effect = TimeoutError()
        with patch("abs_kids_player.web_artwork.client_from_config", return_value=client):
            self.assertIsNone(fetch_artwork(config, "missed"))
            self.assertIsNone(fetch_artwork(config, "missed"))
        client.open_download.assert_called_once()

    def test_now_playing_uses_local_artwork_and_includes_changes_in_live_updates(self):
        status = WebPlayerStatus(title="A book", cover_item_id="book-2")
        payload = player_status_payload(status)
        self.assertEqual(payload["coverUrl"], "/artwork/book-2")
        self.assertIn('src="/artwork/book-2"', render_player_status_value(status))
        changed = player_status_payload(WebPlayerStatus(title="A book", cover_item_id="book-3"))
        self.assertNotEqual(player_status_event_signature(payload), player_status_event_signature(changed))
