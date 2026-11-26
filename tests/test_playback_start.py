from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from abs_kids_player.config import AppConfig
from abs_kids_player.playback_start import (
    PreparedPlayback,
    prepare_book_playback,
)
from abs_kids_player.playback_timing import PlaybackTiming


class PlaybackStartTest(unittest.TestCase):
    def test_prepares_session_with_resume_position_without_starting_a_player(self) -> None:
        config = AppConfig(server_url="https://books.example.com", token="token")
        client = Mock()
        timing = PlaybackTiming(
            selected_at=1.0,
            requested_at=1.1,
            action="continue",
            clock=Mock(side_effect=[1.2, 1.45]),
        )

        with patch("abs_kids_player.playback_start.client_from_config", return_value=client):
            prepared = prepare_book_playback("book-1", config, resume_time=42.5, timing=timing)

        client.start_playback.assert_called_once_with("book-1", start_over=False, resume_time=42.5)
        self.assertEqual(prepared, PreparedPlayback(client.start_playback.return_value, client))
        self.assertEqual(timing.source, "audiobookshelf")
        self.assertAlmostEqual(timing.source_seconds, 0.25)
