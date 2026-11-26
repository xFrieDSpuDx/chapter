from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from abs_kids_player import __version__
from abs_kids_player.audio_output_views import render_audio_output
from abs_kids_player.catalogue_settings import CatalogueStatus
from abs_kids_player.catalogue_views import render_catalogue_settings
from abs_kids_player.config import (
    CLOCK_FORMAT_24_HOUR,
    LIBRARY_SORT_AUTHOR,
    SCREEN_SAVER_CLOCK,
    AppConfig,
    PodcastConfig,
)
from abs_kids_player.connection_health import ConnectionHealth, ConnectionState
from abs_kids_player.player_control import WebPlayerStatus
from abs_kids_player.settings_backup_views import render_settings_restore
from abs_kids_player.setup_views import (
    PAGE,
    SETUP_SCRIPT,
    format_night_mode_status,
    render_connection_health,
    render_health_page,
    render_player_status_value,
    render_podcasts_card,
    render_settings_card,
    render_settings_page,
    render_setup_page,
    render_shelf_settings,
    render_status_panel,
    render_system_card,
    render_wifi_card,
)
from abs_kids_player.wifi import SETUP_HOTSPOT_SSID, WifiStatus


class SetupViewsTest(unittest.TestCase):
    def test_overview_night_mode_reports_the_next_transition_in_the_device_clock_format(self) -> None:
        day = datetime(2026, 6, 9)
        config = AppConfig(night_start="22:00", night_end="07:00")
        self.assertEqual(format_night_mode_status(config, day.replace(hour=22)), "Active · Ends at 7:00 AM tomorrow")
        self.assertEqual(format_night_mode_status(config, day.replace(hour=6, minute=59)), "Active · Ends at 7:00 AM")
        self.assertEqual(format_night_mode_status(config, day.replace(hour=7)), "Off · Starts at 10:00 PM")
        config = replace(config, screen_saver_clock_format=CLOCK_FORMAT_24_HOUR)
        self.assertEqual(format_night_mode_status(config, day.replace(hour=22)), "Active · Ends at 07:00 tomorrow")

    def test_overview_shows_manual_night_modes_and_an_incomplete_schedule(self) -> None:
        self.assertEqual(format_night_mode_status(AppConfig(night_mode="on")), "Active · No scheduled end")
        self.assertEqual(format_night_mode_status(AppConfig(night_mode="off")), "Off")
        self.assertEqual(format_night_mode_status(AppConfig(night_start="22:00")), "Off · Schedule not set")

    def test_screen_saver_clock_choices_preview_the_same_device_time(self) -> None:
        config = AppConfig(
            screen_saver_mode=SCREEN_SAVER_CLOCK, screen_saver_clock_format=CLOCK_FORMAT_24_HOUR
        )
        with patch("abs_kids_player.setup_views.datetime", wraps=datetime) as date:
            date.now.return_value = datetime(2026, 6, 9, 19, 5)
            settings = render_settings_card(config, section="display")

        self.assertIn('name="screen_saver_clock_format"', settings)
        self.assertIn('<option value="12-hour">12-hour (7:05 PM)</option>', settings)
        self.assertIn('<option value="24-hour" selected>24-hour (19:05)</option>', settings)

    def test_health_page_shows_wifi_and_audiobookshelf_success(self) -> None:
        html = render_health_page(
            WifiStatus(connected=True, ssid="Chapter WiFi"),
            ConnectionHealth(ConnectionState.CONNECTED, "chapter"),
        )

        self.assertNotIn("Setup Server", html)
        self.assertIn("Connected to <strong>Chapter WiFi</strong>", html)
        self.assertIn("Library accessible", html)
        self.assertIn("Signed in as chapter.", html)
        self.assertIn(f"<strong>v{__version__}</strong>", html)

    def test_health_page_shows_failures(self) -> None:
        html = render_health_page(
            WifiStatus(connected=False, message="Not connected"),
            ConnectionHealth(ConnectionState.NOT_CONFIGURED),
        )

        self.assertIn("Not connected", html)
        self.assertIn("Not configured", html)

    def test_status_panel_shows_same_status_on_main_page(self) -> None:
        html = render_status_panel(
            WifiStatus(connected=True, ssid="Chapter WiFi"),
            ConnectionHealth(ConnectionState.CONNECTED, "chapter"),
            WebPlayerStatus(
                title="The Hobbit",
                current_time=60,
                duration=300,
                is_playing=True,
                volume_percent=45,
            ),
            (True, "Connected to Playroom Speaker"),
        )

        self.assertNotIn("Setup Server", html)
        self.assertIn("Connected to <strong>Chapter WiFi</strong>", html)
        self.assertIn("Library accessible", html)
        self.assertIn("Signed in as chapter.", html)
        self.assertIn("data-connection-health", html)
        self.assertIn("Bluetooth", html)
        self.assertIn("Connected to <strong>Playroom Speaker</strong>", html)
        self.assertNotIn("Software version", html)
        self.assertIn("Now playing", html)
        self.assertIn("The Hobbit", html)
        self.assertIn("data-now-playing-title", html)
        self.assertIn("data-now-playing-meta", html)
        self.assertIn('<progress data-listening-progress aria-label="Listening progress" max="1" value="0.2">', html)
        self.assertIn("data-playback-form", html)
        self.assertIn("data-playback-action", html)
        self.assertIn("data-playback-button", html)
        self.assertIn('aria-label="Pause on Chapter"', html)
        self.assertIn('class="cover-icon icon-pause" aria-hidden="true"', html)
        self.assertIn('data-playback-pending role="status"', html)
        self.assertNotIn("data-live-source", html)
        self.assertIn('name="action" value="pause"', html)
        self.assertNotIn('name="action" value="play"', html)
        self.assertIn('<label class="volume-label" for="overview-volume">Volume</label>', html)
        self.assertIn("data-volume-percent>45%</div>", html)
        self.assertIn("data-volume-slider", html)
        self.assertIn('name="volume" type="range"', html)
        self.assertNotIn(">Set</button>", html)

    def test_overview_groups_player_and_connection_status_into_rows(self) -> None:
        page = render_status_panel(WifiStatus(False), ConnectionHealth(ConnectionState.CHECKING))
        self.assertEqual(page.count('<dl class="status row-dividers">'), 2)
        self.assertEqual(page.count('<div class="status-row">'), 5)

    def test_nested_admin_pages_link_to_their_menu(self) -> None:
        cases = (
            (render_shelf_settings([], "", "csrf"), "/settings/books", "Books"),
            (render_catalogue_settings(AppConfig(), CatalogueStatus(), "csrf"), "/settings/books", "Books"),
            (render_audio_output(WebPlayerStatus(), "Not connected", "csrf"), "/?tab=connections", "Connections"),
            (render_settings_restore("csrf"), "/?tab=system", "System"),
        )
        for page, url, label in cases:
            with self.subTest(url=url):
                self.assertIn(f'class="text-action back-link" href="{url}">{label}</a>', page)

    def test_status_panel_shows_bluetooth_disconnected_by_default(self) -> None:
        html = render_status_panel(
            WifiStatus(connected=True, ssid="Chapter WiFi"),
            ConnectionHealth(ConnectionState.CONNECTED, "chapter"),
        )

        self.assertIn("Bluetooth", html)
        self.assertIn("Not connected", html)

    def test_connection_health_shows_checked_time_and_recovery_action(self) -> None:
        checked_at = datetime(2026, 11, 23, 19, 5, tzinfo=timezone.utc)
        result = render_connection_health(
            ConnectionHealth(ConnectionState.LIBRARY_UNAVAILABLE, "<listener>", checked_at)
        )

        self.assertIn("Library unavailable", result)
        self.assertIn("Signed in as &lt;listener&gt;.", result)
        self.assertIn("Check that this account can access the library", result)
        self.assertIn('Last checked <time datetime="2026-11-23T19:05:00+00:00">', result)

    def test_player_status_value_shows_nothing_playing_without_session(self) -> None:
        html = render_player_status_value(WebPlayerStatus())

        self.assertIn("Nothing playing", html)
        self.assertIn('data-has-session="0"', html)
        self.assertIn('class="cover-with-controls"', html)
        self.assertIn('class="volume-form hidden"', html)
        self.assertIn('action="/player"', html)

    def test_player_status_value_shows_play_button_when_paused(self) -> None:
        html = render_player_status_value(
            WebPlayerStatus(
                title="The Hobbit",
                current_time=60,
                duration=300,
                is_playing=False,
                volume_percent=45,
            )
        )

        self.assertIn('name="action" value="play"', html)
        self.assertIn('aria-label="Play on Chapter"', html)
        self.assertIn('class="cover-icon icon-play" aria-hidden="true"', html)
        self.assertNotIn('name="action" value="pause"', html)

    def test_settings_page_groups_preferences_under_one_save_per_section(self) -> None:
        config = AppConfig(
            control_click_enabled=True,
            spoken_navigation_enabled=False,
            library_sort_mode=LIBRARY_SORT_AUTHOR,
            playback_speed=1.5,
            screen_saver_mode=SCREEN_SAVER_CLOCK,
            screen_saver_dim_percent=5,
            sleep_timer_enabled=True,
            sleep_timer_minutes=45,
            night_mode="on",
        )
        for section in ("sound", "display", "listening", "books"):
            with self.subTest(section=section):
                page = render_settings_page(config, section, "test-csrf", saved=True)
                self.assertEqual(page.count('method="post"'), 1)
                self.assertIn(f'action="/settings/{section}"', page)
                self.assertIn('name="csrf_token" value="test-csrf"', page)
                self.assertIn("Settings saved.", page)
                self.assertIn('href="/?tab=settings">All settings</a>', page)

        sound = render_settings_card(config, section="sound")
        self.assertIn('name="control_click_enabled" value="1" checked', sound)
        self.assertIn('name="spoken_navigation_enabled" value="1">', sound)
        self.assertNotIn('screen_saver_mode', sound)
        display = render_settings_card(config, section="display")
        self.assertIn('<option value="clock" selected>Digital clock</option>', display)
        self.assertIn('<option value="5" selected>5% (darkest)</option>', display)
        listening = render_settings_card(config, section="listening")
        self.assertIn('<option value="on" selected>On</option>', listening)
        self.assertIn('name="sleep_timer_enabled" value="1" checked', listening)
        self.assertIn('name="sleep_timer_minutes"', listening)
        self.assertIn('value="45"', listening)
        books = render_settings_card(config, section="books")
        self.assertIn('<option value="author" selected>Author (last name)</option>', books)
        self.assertIn('<option value="1.5" selected>1.5x</option>', books)
        self.assertIn('href="/settings/shelves"', books)

    def test_podcasts_card_shows_saved_podcasts_and_apple_instruction(self) -> None:
        html = render_podcasts_card(
            AppConfig(
                podcasts=[
                    PodcastConfig(
                        url="https://podcasts.apple.com/us/podcast/yoto-daily/id1635154611",
                        title="Yoto Daily",
                        author="Yoto",
                    )
                ]
            )
        )

        self.assertIn('<h2 id="podcasts-heading">Podcast feeds</h2>', html)
        self.assertIn("Paste the show link from podcasts.apple.com", html)
        self.assertIn('action="/podcasts"', html)
        self.assertIn('name="podcast_url"', html)
        self.assertIn("Yoto Daily by Yoto", html)
        self.assertIn("Add row", html)
        self.assertIn("Save Podcasts", html)

    def test_audiobookshelf_form_does_not_show_library_id(self) -> None:
        self.assertNotIn("Library ID", PAGE)
        self.assertNotIn("library_id", PAGE)

    def test_admin_navigation_supports_direct_links_and_highlights_the_current_page(self) -> None:
        page = render_setup_page(
            config=AppConfig(),
            wifi=WifiStatus(connected=True),
            abs_status=ConnectionHealth(ConnectionState.CONNECTED, "chapter"),
            player_status=WebPlayerStatus(),
            bt_status=(False, "Not connected"),
            csrf_token="token",
            active_tab="system",
        )

        self.assertIn("<title>Chapter Player</title>", page)
        self.assertIn('class="app-brand"', page)
        self.assertIn('class="app-logo" src="/assets/chapter-logo.png"', page)
        self.assertIn('<a class="skip-link" href="#main-content">Skip to content</a>', page)
        self.assertIn('<nav class="admin-nav" aria-label="Player administration">', page)
        self.assertIn('<a href="/library">Library</a>', page)
        self.assertIn('<a href="/library?downloaded=1&amp;section=downloads">Downloads</a>', page)
        self.assertIn('<a href="/?tab=system" aria-current="page" data-tab-target="system">System</a>', page)
        self.assertIn('data-tab-panel="system">', page)
        self.assertIn("<h1>System</h1>", page)
        self.assertNotIn('data-tab-target="reset"', page)
        for tab_name in ("overview", "settings", "podcasts", "connections"):
            self.assertIn(f'data-tab-target="{tab_name}"', page)
            self.assertIn(f'data-tab-panel="{tab_name}" hidden>', page)

    def test_system_card_shows_version_and_uses_in_page_confirmations(self) -> None:
        html = render_system_card()

        self.assertIn('<h2 id="system-heading">Software and recovery</h2>', html)
        self.assertIn("Software version", html)
        self.assertIn(f'<strong>v{__version__}</strong>', html)
        self.assertNotIn("<h3>Software Update</h3>", html)
        self.assertIn("data-system-update-status", html)
        self.assertIn("data-system-update-description", html)
        self.assertIn('data-confirm-trigger="update"', html)
        self.assertIn('data-confirm-panel="update" hidden', html)
        self.assertIn('action="/system/update"', html)
        self.assertIn("Restore Device to Default Settings", html)
        self.assertIn('<a class="resource-link download-link" href="/system/settings-backup" download>', html)
        self.assertIn('<h2 id="device-actions-heading">Device actions</h2>', html)
        self.assertIn('data-confirm-trigger="reboot"', html)
        self.assertIn('data-confirm-panel="reboot" hidden', html)
        self.assertIn("Are you sure you want to reboot?", html)
        self.assertIn('action="/system/reboot"', html)
        self.assertIn('data-confirm-trigger="restore"', html)
        self.assertIn('data-confirm-panel="restore" hidden', html)
        self.assertIn("Are you sure you want to restore the device to default settings?", html)
        self.assertIn('action="/system/reset"', html)
        self.assertNotIn("window.confirm", SETUP_SCRIPT)
        self.assertIn('request.open("GET", "/system/update/status", true)', SETUP_SCRIPT)
        self.assertIn("refreshSystemUpdateStatus()", SETUP_SCRIPT)
        self.assertIn("softwareUpdateWasInstalling = true", SETUP_SCRIPT)
        self.assertIn('statusText.textContent = "Finishing update..."', SETUP_SCRIPT)
        self.assertIn("softwareUpdateWasInstalling ? 2000", SETUP_SCRIPT)
        self.assertIn('window.location.replace("/")', SETUP_SCRIPT)
        self.assertIn("description.hidden = !description.textContent.trim()", SETUP_SCRIPT)
        self.assertNotIn("This player is running the latest stable version of Chapter.", PAGE)

    def test_admin_page_uses_sse_with_polling_fallback(self) -> None:
        self.assertIn('new EventSource("/player/events")', SETUP_SCRIPT)
        self.assertIn("source.onmessage", SETUP_SCRIPT)
        self.assertNotIn('addEventListener("status"', SETUP_SCRIPT)
        self.assertNotIn("Live updates:", SETUP_SCRIPT)
        self.assertNotIn('http-equiv="refresh"', PAGE)
        self.assertNotIn("window.setInterval(refreshPlayerStatus, 5000)", SETUP_SCRIPT)
        self.assertIn(
            "fallbackPollTimer = window.setInterval(refreshPlayerStatus, 1000)", SETUP_SCRIPT
        )
        self.assertIn("pendingPlaybackAction", SETUP_SCRIPT)
        self.assertIn("setPendingPlayback", SETUP_SCRIPT)
        self.assertNotIn("applyOptimisticPlayback", SETUP_SCRIPT)

    def test_wifi_card_shows_forget_button_when_connected_to_home_wifi(self) -> None:
        html = render_wifi_card(WifiStatus(connected=True, ssid="Chapter WiFi"))

        self.assertIn("Connected to <strong>Chapter WiFi</strong>", html)
        self.assertIn("open Wi-Fi network", html)
        self.assertIn("Forget this WiFi network", html)
        self.assertNotIn("password", html.lower())
        self.assertNotIn('action="/wifi"', html)

    def test_wifi_card_shows_setup_form_when_on_setup_hotspot(self) -> None:
        html = render_wifi_card(WifiStatus(connected=True, ssid=SETUP_HOTSPOT_SSID))

        self.assertIn("open Wi-Fi network", html)
        self.assertIn("Connect to Wi-Fi", html)
        self.assertIn('action="/wifi"', html)
        self.assertNotIn("Forget this WiFi network", html)
