from __future__ import annotations

import argparse
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import threading
import time
from dataclasses import asdict, replace
from hashlib import sha256
from http.server import ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, quote, urlencode, urlparse

from . import __version__
from .api import AudiobookshelfClient, AudiobookshelfError
from .audio_output_test import AUDIO_TEST_COMMAND, AUDIO_TEST_OUTPUTS
from .audio_output_views import render_audio_output
from .bluetooth_audio import connected_bluetooth_audio_device
from .book_details_views import book_metadata, render_book_details
from .bookmarks import bookmarks_for_item
from .build_info import log_installed_build
from .catalogue_settings import (
    CATALOGUE_COMMAND,
    available_book_libraries,
    catalogue_request_key,
    catalogue_status_key,
    format_last_refresh,
    load_catalogue_status,
)
from .catalogue_views import render_catalogue_settings
from .config import (
    NIGHT_MODES,
    AppConfig,
    load_config,
    same_login,
    save_config,
    update_config,
    valid_clock_time,
    valid_feedback_volume,
    valid_library_sort_mode,
    valid_playback_speed,
    valid_skip_seconds,
    valid_sleep_timer_minutes,
)
from .connection_health import ConnectionMonitor
from .device_preferences import (
    save_click_setting,
    save_playback_speed_setting,
    save_screen_saver_setting,
    save_sleep_timer_setting,
    save_spoken_navigation_setting,
)
from .download_assets import read_details
from .download_routes import change_download, change_download_settings, download_status_payload
from .download_space import DownloadSpace
from .download_store import (
    book_directory,
    books_with_downloads,
    completed_download_ids,
    read_download,
    web_downloads,
)
from .download_views import (
    download_space_label,
    library_url,
    render_library,
)
from .downloads import downloads
from .library import available_shelves, series_key
from .library_cache import clear_cached_books, load_cached_books
from .listening_control import control_value
from .listening_queue import edit_queue, read_queue
from .listening_status import (
    listening_details,
    listening_progress,
    listening_source,
    matches_listening_account,
)
from .owner_auth import (
    OwnerAccess,
    OwnerSessions,
    change_owner_login,
    change_public_access,
    session_cookie,
)
from .owner_views import render_owner_page, render_security_page
from .player_control import (
    WebPlayerStatus,
    command_result,
    load_web_player_status,
    queue_web_player_command,
)
from .player_state import clear_last_playback
from .player_views import render_player_page, render_player_queue
from .podcast import PodcastError, resolve_podcast_config
from .podcast_catalogue import load_podcast_catalogue
from .progress_sync import progress_account, read_listening_progress
from .settings_backup import (
    BACKUP_VERSION,
    BackupError,
    apply_preview,
    backup_payload,
    preview_ticket,
    read_backup,
)
from .settings_backup_views import render_settings_restore
from .setup_http import SetupHTTPHandler, one
from .setup_views import (
    ASSET_DIR,
    CSRF_FIELD_NAME,
    SETTINGS_PAGES,
    format_download_activity,
    format_night_mode_status,
    overview_listening_status,
    render_bluetooth_status_value,
    render_connection_health,
    render_guest_overview,
    render_health_page,
    render_settings_page,
    render_setup_page,
    render_shelf_settings,
    render_wifi_card,
    render_wifi_status_value,
    wifi_card_key,
)
from .software_update import (
    UPDATE_SERVICE_NAME,
    SoftwareUpdateState,
    load_update_state,
    save_update_state,
    update_state_is_active,
    update_status_payload,
    version_tuple,
    wait_for_update_display_ready,
)
from .system_diagnostics import collect_system_diagnostics, diagnostic_payload
from .system_status_views import render_system_status
from .theme_views import render_themes_page
from .web_artwork import fetch_artwork
from .web_catalogue import CatalogueLoader
from .web_device_status import BluetoothStatusCache, WifiStatusCache
from .web_library import (
    BROWSE_VIEWS,
    browser_shelves,
    find_library_book,
    library_display_filters,
    library_page,
    shelf_groups,
)
from .web_player_events import player_status_payload, stream_player_events
from .web_reads import WebReads
from .wifi import (
    SETUP_HOTSPOT_SSID,
    SETUP_HOTSPOT_URL,
    WifiError,
    connect_wifi,
    ensure_wifi_or_hotspot,
    forget_all_wifi_connections_and_start_hotspot,
    forget_current_wifi_and_start_hotspot,
    wifi_status,
)


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 47831
WIFI_CHANGE_DELAY_SECONDS = 5.0
SYSTEM_ACTION_DELAY_SECONDS = 3.0
LOGO_PATH = ASSET_DIR / "chapter-logo.png"
connection_monitor = ConnectionMonitor()
update_request_lock = threading.Lock()
owner_sessions = OwnerSessions()
web_catalogue = CatalogueLoader()
web_bluetooth = BluetoothStatusCache()
web_wifi = WifiStatusCache()
web_reads = WebReads()
download_space = DownloadSpace()


def download_activity_status(config: AppConfig) -> dict[str, str]:
    completed = completed_download_ids(web_downloads(config))
    activity = downloads.activity(config, completed)
    return format_download_activity(
        len(completed), activity.downloading, activity.in_queue,
        download_space_label(download_space.reading(config).remaining),
    )


def guest_can_read(path: str, fields: dict[str, list[str]], sections: tuple[str, ...]) -> bool:
    if "overview" in sections:
        if path == "/status/overview" or (path == "/" and one(fields, "tab") in ("", "overview")):
            return True
    return "library" in sections and (
        path in ("/library", "/library/details", "/library/download/status") or path.startswith("/artwork/")
    )


class SetupHandler(SetupHTTPHandler):
    server_version = f"ChapterPlayer/{__version__}"
    protocol_version = "HTTP/1.1"

    def handle_get(self) -> None:
        url = urlparse(self.path)
        path = url.path
        fields = parse_qs(url.query)
        if path == "/owner":
            self.send_owner_page(updated=one(fields, "updated") == "1")
            return
        if path == "/assets/chapter-logo.png":
            self.send_logo_png()
            return
        access = owner_sessions.access(self.owner_cookie())
        if path == "/player/status":
            # The local updater uses the display timestamp to confirm an update.
            if access.can_manage:
                self.send_player_status_json()
            else:
                self.send_json({"displayDrawnAt": load_web_player_status().display_drawn_at})
            return
        guest_sections = None if access.can_manage else access.public_sections
        if guest_sections is not None and not guest_can_read(path, fields, guest_sections):
            if path == "/" and not one(fields, "tab") and "library" in guest_sections:
                self.redirect_owner("/library")
            else:
                self.send_owner_required()
            return
        if path.startswith("/artwork/"):
            self.send_artwork(path.removeprefix("/artwork/"), full=one(fields, "size") == "detail")
            return
        if path.startswith("/settings/"):
            self.handle_settings_get(path, fields, access)
            return
        if path == "/player" or path.startswith("/player/"):
            self.handle_player_get(path, fields)
            return
        if path.startswith("/system/"):
            self.handle_system_get(path)
            return
        if path == "/status/downloads":
            self.send_json(download_activity_status(load_config()))
            return
        if path == "/status/overview":
            config = load_config()
            summary = {**download_activity_status(config), "nightMode": format_night_mode_status(config)}
            status = load_web_player_status()
            if guest_sections is not None:
                summary.update(overview_listening_status(status))
            elif status.has_session:
                progress = listening_progress(config, status)
                label = progress.get("label", "") if progress.get("state") != "not_saved" else ""
                summary["listeningSource"] = " · ".join(filter(None, (listening_source(status), label)))
            else:
                summary["listeningSource"] = ""
            self.send_json(summary)
            return
        if path == "/status/wifi":
            wifi = web_wifi.current(wifi_status)
            data = {"label": render_wifi_status_value(wifi), "connected": wifi.connected if wifi else None}
            if one(fields, "card") == "1":
                data["cardKey"] = wifi_card_key(wifi)
                data["card"] = render_wifi_card(wifi, self.csrf_token())
            self.send_json(data)
            return
        if path == "/status/bluetooth":
            connected, message = web_bluetooth.current(bluetooth_status)
            self.send_json({"connected": connected, "label": render_bluetooth_status_value(message)})
            return
        if path == "/status/audiobookshelf":
            self.send_html(200, render_connection_health(connection_monitor.status_for(load_config())))
            return
        if path == "/health":
            self.send_health_page()
            return
        if path == "/library/details":
            self.send_book_details(fields, guest_sections=guest_sections)
            return
        if path == "/library/download/status":
            self.send_download_status(fields, can_manage=access.can_manage)
            return
        if path == "/library":
            self.send_library(fields, guest_sections=guest_sections)
            return
        if path != "/":
            self.send_error(404)
            return
        if guest_sections is not None:
            config = load_config()
            self.send_html(200, render_guest_overview(
                config, load_web_player_status(), download_activity_status(config), guest_sections,
            ))
        else:
            self.send_page(active_tab=one(fields, "tab"))

    def handle_settings_get(self, path: str, fields: dict[str, list[str]], access: OwnerAccess) -> None:
        if path == "/settings/security":
            self.send_html(200, render_security_page(
                self.csrf_token(), access.credential, saved=one(fields, "saved") == "1",
            ))
            return
        if path == "/settings/themes":
            self.send_html(200, render_themes_page())
            return
        section = path.removeprefix("/settings/")
        if section in SETTINGS_PAGES:
            self.send_html(200, render_settings_page(
                load_config(), section, self.csrf_token(),
                saved=one(fields, "saved") == "1",
            ))
            return
        if path == "/settings/audio-output":
            request_id = one(fields, "request")
            if not re.fullmatch(r"[0-9a-f]{32}", request_id):
                request_id = ""
            self.send_html(200, render_audio_output(
                load_web_player_status(), bluetooth_status()[1], self.csrf_token(),
                request_id, command_result(request_id) if request_id else "",
            ))
            return
        if path == "/settings/shelves":
            self.send_shelf_settings()
            return
        if path == "/settings/catalogue":
            self.send_catalogue_settings(one(fields, "request")[:64])
            return
        if path == "/settings/catalogue/options":
            self.send_catalogue_options()
            return
        if path == "/settings/catalogue/result":
            self.send_catalogue_result(one(fields, "request")[:64])
            return
        self.send_error(404)

    def handle_player_get(self, path: str, fields: dict[str, list[str]]) -> None:
        if path == "/player/events":
            self.send_player_events()
            return
        if path == "/player":
            config = load_config()
            status = load_web_player_status()
            self.send_html(200, render_player_page(config, status, self.listening_queue_markup(config, status), self.csrf_token()))
            return
        if path == "/player/state":
            self.send_listening_state()
            return
        if path == "/player/bookmarks":
            self.send_player_bookmarks(one(fields, "session_key"), one(fields, "version")[:12])
            return
        if path == "/player/queue/status":
            self.send_json({"markup": self.listening_queue_markup(load_config(), load_web_player_status())})
            return
        if path == "/player/command":
            self.send_json({"outcome": command_result(one(fields, "id")), "observedAt": time.time()})
            return
        self.send_error(404)

    def handle_system_get(self, path: str) -> None:
        if path == "/system/update/status":
            self.send_system_update_status_json()
            return
        if path == "/system/status":
            self.send_html(200, render_system_status(collect_system_diagnostics(load_config())))
            return
        if path == "/system/settings-backup":
            self.send_json(backup_payload(load_config()), attachment="chapter-settings.json")
            return
        if path == "/system/settings-restore":
            self.send_html(200, render_settings_restore(self.csrf_token()))
            return
        if path == "/system/diagnostics":
            self.send_json(
                diagnostic_payload(collect_system_diagnostics(load_config())),
                attachment="chapter-diagnostics.json",
            )
            return
        self.send_error(404)

    def send_logo_png(self) -> None:
        body = LOGO_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_artwork(self, item_id: str, *, full: bool = False) -> None:
        artwork = fetch_artwork(load_config(), item_id, full=full)
        etag = f'"{sha256(artwork[1]).hexdigest()}"' if artwork else ""
        unchanged = bool(etag and getattr(self, "headers", {}).get("If-None-Match") == etag)
        self.send_response(304 if unchanged else 200 if artwork else 404)
        self.send_header("Cache-Control", "private, no-cache" if artwork else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if artwork:
            self.send_header("ETag", etag)
        if unchanged:
            self.end_headers()
            return
        if artwork:
            self.send_header("Content-Type", artwork[0])
            self.send_header("Content-Length", str(len(artwork[1])))
        else:
            self.send_header("Content-Length", "0")
        self.end_headers()
        if artwork:
            self.wfile.write(artwork[1])

    def handle_post(self) -> None:
        path = urlparse(self.path).path
        handlers = {
            "/owner/sign-in": self.handle_owner_sign_in,
            "/owner/sign-out": self.handle_owner_sign_out,
            "/settings/security/account": self.handle_owner_account_post,
            "/settings/security/access": self.handle_visitor_access_post,
            "/wifi": self.handle_wifi_post,
            "/wifi/forget": self.handle_forget_wifi_post,
            "/player": self.handle_player_post,
            "/player/control": self.handle_listening_control_post,
            "/player/queue": self.handle_listening_queue_post,
            "/settings/sound": self.handle_sound_settings_post,
            "/settings/books": self.handle_book_settings_post,
            "/settings/display": self.handle_display_settings_post,
            "/settings/click": self.handle_click_setting_post,
            "/settings/spoken-navigation": self.handle_spoken_navigation_setting_post,
            "/settings/library-order": self.handle_library_sort_setting_post,
            "/settings/shelves": self.handle_shelf_setting_post,
            "/settings/catalogue": self.handle_catalogue_post,
            "/settings/downloads": self.handle_library_download_settings,
            "/library/settings": self.handle_library_download_settings,
            "/library/download": self.handle_library_download,
            "/settings/playback-speed": self.handle_playback_speed_setting_post,
            "/settings/screensaver": self.handle_screen_saver_setting_post,
            "/settings/listening": self.handle_listening_setting_post,
            "/settings/sleep": self.handle_sleep_setting_post,
            "/podcasts": self.handle_podcasts_post,
            "/system/reboot": self.handle_system_reboot_post,
            "/system/update": self.handle_system_update_post,
            "/system/reset": self.handle_system_reset_post,
            "/system/settings-preview": self.handle_settings_preview_post,
            "/system/settings-restore": self.handle_settings_restore_post,
            "/settings/night-mode": self.handle_night_mode_post,
            "/settings/audio-test": self.handle_audio_test_post,
            "/login": self.handle_login_post,
        }
        handler = handlers.get(path)
        if handler is None:
            self.send_error(404)
            return

        fields = self.post_fields()
        if fields is None:
            return
        submitted_token = one(fields, CSRF_FIELD_NAME)
        if not (
            submitted_token and submitted_token.isascii()
            and hmac.compare_digest(submitted_token, self.csrf_token())
        ):
            self.send_csrf_error()
            return
        if path != "/owner/sign-in" and not self.owner_authorized():
            self.send_owner_required()
            return
        handler()

    def owner_cookie(self) -> str:
        return getattr(self, "headers", {}).get("Cookie", "")

    def owner_authorized(self) -> bool:
        return owner_sessions.allows(self.owner_cookie())

    def send_owner_required(self) -> None:
        if getattr(self, "headers", {}).get("X-Requested-With") == "fetch":
            self.send_json({"error": "Sign in to Chapter as owner."}, status=401)
            return
        self.redirect_owner("/owner")

    def send_owner_page(self, *, error: str = "", status: int = 200, updated: bool = False) -> None:
        access = owner_sessions.access(self.owner_cookie())
        if access.unreadable:
            error = "Owner access could not be read. Reset the owner password on the Pi."
        self.send_html(status, render_owner_page(
            self.csrf_token(), configured=access.credential is not None or access.unreadable,
            signed_in=access.signed_in, error=error, updated=updated,
            guest_sections=None if access.can_manage else access.public_sections,
        ))

    def handle_owner_sign_in(self) -> None:
        fields = self.post_fields() or {}
        token = owner_sessions.sign_in(one(fields, "username"), fields.get("password", [""])[0])
        if token:
            self.redirect_owner("/?tab=overview", cookie=session_cookie(token))
        else:
            self.send_owner_page(error="Could not sign in. Wait a moment and try again.", status=401)

    def handle_owner_sign_out(self) -> None:
        owner_sessions.sign_out(self.owner_cookie())
        self.redirect_owner("/owner", cookie=session_cookie(""))

    def handle_owner_account_post(self) -> None:
        access = owner_sessions.access(self.owner_cookie())
        if not access.can_manage:
            self.send_owner_required()
            return
        fields = self.post_fields() or {}
        password = fields.get("password", [""])[0]
        try:
            if password != fields.get("confirm_password", [""])[0]:
                raise ValueError("The new passwords differ.")
            change_owner_login(
                one(fields, "username"), password, expected=access.credential,
                current_password=fields.get("current_password", [""])[0],
            )
        except (OSError, ValueError) as error:
            self.send_security_error(str(error) if isinstance(error, ValueError) else "Could not save the owner account.")
            return
        self.redirect_owner("/owner?updated=1", cookie=session_cookie(""))

    def handle_visitor_access_post(self) -> None:
        access = owner_sessions.access(self.owner_cookie())
        if not access.can_manage:
            self.send_owner_required()
            return
        if access.credential is None:
            self.send_security_error("Set up the owner account first.")
            return
        fields = self.post_fields() or {}
        try:
            change_public_access(
                access.credential, overview=one(fields, "public_overview") == "1",
                library=one(fields, "public_library") == "1",
            )
        except (OSError, ValueError) as error:
            self.send_security_error(str(error) if isinstance(error, ValueError) else "Could not save visitor access.")
            return
        self.redirect_settings("security")

    def send_security_error(self, message: str) -> None:
        access = owner_sessions.access(self.owner_cookie())
        if access.can_manage:
            self.send_html(400, render_security_page(self.csrf_token(), access.credential, error=message))
        else:
            self.send_owner_required()

    def redirect_owner(self, location: str, *, cookie: str = "") -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def csrf_token(self) -> str:
        return getattr(getattr(self, "server", None), "csrf_token", "")

    def handle_wifi_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        ssid = one(fields, "wifi_ssid")
        password = one(fields, "wifi_password")

        if not ssid:
            self.send_page(message="Wi-Fi network name is required.", is_error=True, active_tab="connections")
            return

        schedule_wifi_connect(ssid, password)
        self.send_page(
            message=(
                "Wi-Fi change submitted. The player will switch networks in a moment. "
                "Hold the volume knob for 10 seconds to show the new setup address."
            ),
            active_tab="connections",
        )

    def handle_forget_wifi_post(self) -> None:
        schedule_wifi_forget()
        self.send_page(
            message=(
                f"Wi-Fi forget submitted. In about 5 seconds, connect to "
                f"{html.escape(SETUP_HOTSPOT_SSID)}, then browse to {html.escape(SETUP_HOTSPOT_URL)}."
            ),
            active_tab="connections",
        )

    def handle_login_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        server_url = one(fields, "server_url")
        username = one(fields, "username")
        password = one(fields, "password")

        try:
            saved_config = login_and_refresh_player(server_url, username, password)
        except AudiobookshelfError as error:
            self.send_page(
                message=f"Login failed: {html.escape(str(error))}",
                is_error=True,
                server_url=server_url,
                active_tab="connections",
            )
            return

        self.send_page(
            message="Player login saved. You can close this page and restart the player.",
            server_url=saved_config.server_url,
            active_tab="connections",
        )

    def handle_player_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        action = one(fields, "action")

        if action == "pause_book":
            config = load_config()
            item_id = one(fields, "book_id")
            status = load_web_player_status()
            if not config.is_ready or not item_id or status.cover_item_id != item_id:
                self.send_json({"error": "Chapter is playing a different book."}, status=409)
                return
            command = queue_web_player_command("pause_book", catalogue_key=catalogue_request_key(config), book_id=item_id)
            self.send_player_command_response(command.id)
            return
        if action == "play_book":
            series = one(fields, "group") if one(fields, "view") == "series" else ""
            self.handle_play_on_chapter(fields, series=series)
            return
        if action in {"play", "pause"}:
            self.send_player_command_response(queue_web_player_command(action).id)
            return

        if action == "volume":
            try:
                volume = min(max(int(one(fields, "volume")), 0), 100)
            except ValueError:
                self.send_page(message="Volume must be between 0 and 100.", is_error=True)
                return
            self.send_player_command_response(queue_web_player_command("volume", volume).id)
            return

        self.send_page(message="Unknown player command.", is_error=True)

    def send_listening_state(self) -> None:
        status = load_web_player_status()
        config = load_config()
        matching_account = matches_listening_account(config, status)
        self.send_json({
            **player_status_payload(status),
            **listening_details(status),
            "sessionKey": status.session_key if matching_account else "",
            "source": listening_source(status),
            "output": {"speaker": "Speaker", "bluetooth": "Bluetooth"}.get(status.audio_output, ""),
            "muted": status.muted,
            "speed": round((status.playback_rate if status.session_key else config.playback_speed) * 100),
            "skipBack": config.skip_back_seconds,
            "skipForward": config.skip_forward_seconds,
            "canBookmark": bool(status.account and matching_account and not status.episode_id),
            "progress": listening_progress(config, status) or None,
        })

    def handle_listening_control_post(self) -> None:
        fields = self.post_fields() or {}
        key = one(fields, "session_key")
        status = load_web_player_status()
        config = load_config()
        action = one(fields, "action")
        if action == "cancel_open":
            # A Library click can be cancelled before the OLED publishes its opening status.
            if len(key) != 32 or any(char not in "0123456789abcdef" for char in key):
                self.send_json({"error": "This opening request has changed."}, status=409)
                return
            self.send_player_command_response(queue_web_player_command(
                action, session_key=key, catalogue_key=catalogue_status_key(config),
            ).id)
            return
        if not key or key != status.session_key or not matches_listening_account(config, status):
            self.send_json({"error": "The active book has changed. Try again."}, status=409)
            return
        try:
            value = control_value(action, one(fields, "value"), status)
        except (ValueError, OverflowError):
            self.send_json({"error": "That playback control or value is unavailable."}, status=400)
            return
        self.send_player_command_response(queue_web_player_command(
            action, value, session_key=key, catalogue_key=catalogue_status_key(config),
        ).id)

    def send_player_bookmarks(self, key: str, version: str = "") -> None:
        status = load_web_player_status()
        config = load_config()
        if not key or key != status.session_key or status.episode_id or not status.account or status.account != progress_account(config):
            self.send_json({"error": "Bookmarks are unavailable for the active book."}, status=409)
            return
        rows, pending = web_reads.get(
            ("player-bookmarks", key, version),
            lambda: bookmarks_for_item(config, status.cover_item_id, status.duration),
        )
        if rows is None and not pending:
            self.send_json({"error": "Could not load bookmarks from Audiobookshelf."}, status=503)
            return
        self.send_json({"pending": pending, "bookmarks": [asdict(bookmark) for bookmark in rows or []]})

    def listening_queue_markup(self, config: AppConfig, status: WebPlayerStatus) -> str:
        order = read_queue(catalogue_status_key(config))
        active = ""
        if status.account == progress_account(config) and status.library_id == config.library_id and not status.episode_id:
            active = status.cover_item_id
        books = []
        if config.is_ready and (active or order.items):
            books = books_with_downloads(config, load_cached_books(config), copies=web_downloads(config))
        return render_player_queue(order, books, active, self.csrf_token(), listening_details(status).get("series", ""))

    def handle_listening_queue_post(self) -> None:
        fields = self.post_fields() or {}
        action = one(fields, "action")
        item = one(fields, "book_id")
        config = load_config()
        key = catalogue_status_key(config)
        if not config.is_ready or action not in {"add", "remove", "up", "down", "auto", "play"}:
            self.send_listening_error(400, "Choose an action for your Audiobookshelf listening queue.")
            return
        if action == "play":
            self.handle_play_on_chapter(fields, series=one(fields, "series"))
            return
        if action == "add" and not any(
            book.id == item and not book.episode_id and not book.id.startswith("podcast:")
            for book in books_with_downloads(config, load_cached_books(config))
        ):
            self.send_listening_error(404, "This book is not in the current library.")
            return
        try:
            edit_queue(key, action, one(fields, "enabled") if action == "auto" else item)
        except ValueError as error:
            self.send_listening_error(409, str(error))
            return
        except OSError:
            self.send_listening_error(503, "Could not save the listening queue. Try again.")
            return
        if self.headers.get("X-Requested-With") == "fetch":
            self.send_json({"saved": True})
        else:
            self.send_response(303)
            self.send_header("Location", "/player#listening-queue")
            self.send_header("Content-Length", "0")
            self.end_headers()

    def send_listening_error(self, status: int, message: str) -> None:
        if self.headers.get("X-Requested-With") == "fetch":
            self.send_json({"error": message}, status=status)
        else:
            self.send_request_error(status, message)

    def handle_play_on_chapter(self, fields: dict[str, list[str]], *, series: str = "") -> None:
        item_id = one(fields, "book_id")
        config = load_config()
        book = next((
            book for book in books_with_downloads(config, load_cached_books(config))
            if book.id == item_id and not book.episode_id and not book.id.startswith("podcast:")
        ), None) if config.is_ready else None
        if book is None:
            message = "Choose a book from the current library."
            if getattr(self, "headers", {}).get("X-Requested-With") == "fetch":
                self.send_json({"error": message}, status=409)
            else:
                self.send_request_error(409, message)
            return
        command = queue_web_player_command(
            "play_book", catalogue_key=catalogue_request_key(config), book_id=book.id,
            resume_time=book.current_time,
            local_only=library_display_filters(fields)["section"] == "downloads" and (
                one(fields, "downloaded") == "1" or book.downloaded
            ),
            **({"series": series} if series and any(series_key(member) == series for member in book.series) else {}),
        )
        self.send_player_command_response(command.id)

    def handle_sound_settings_post(self) -> None:
        fields = self.post_fields() or {}
        update_config(lambda config: replace(
            config,
            control_click_enabled=one(fields, "control_click_enabled") == "1",
            spoken_navigation_enabled=one(fields, "spoken_navigation_enabled") == "1",
            control_click_volume_percent=valid_feedback_volume(
                one(fields, "control_click_volume_percent"), config.control_click_volume_percent,
            ),
            spoken_navigation_volume_percent=valid_feedback_volume(
                one(fields, "spoken_navigation_volume_percent"), config.spoken_navigation_volume_percent,
            ),
        ))
        self.redirect_settings("sound")

    def handle_book_settings_post(self) -> None:
        fields = self.post_fields() or {}
        previous, updated = update_config(lambda config: replace(
            config,
            library_sort_mode=valid_library_sort_mode(one(fields, "library_sort_mode")),
            playback_speed=valid_playback_speed(one(fields, "playback_speed")),
        ))
        if previous.library_sort_mode != updated.library_sort_mode:
            queue_web_player_command("resort_library")
        self.redirect_settings("books")

    def handle_display_settings_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        try:
            save_screen_saver_setting(
                one(fields, "screen_saver_mode"), one(fields, "screen_saver_dim_percent"),
                clock_format=one(fields, "screen_saver_clock_format"),
                screen_off_minutes=one(fields, "screen_off_minutes") if "screen_off_minutes" in fields else None,
                screen_off_hours=(one(fields, "screen_off_start"), one(fields, "screen_off_end"))
                if "screen_off_start" in fields or "screen_off_end" in fields else None,
            )
        except ValueError as error:
            self.send_html(400, render_settings_page(load_config(), "display", self.csrf_token(), error=str(error)))
            return
        self.redirect_settings("display")

    def handle_click_setting_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        if "volume_percent" in fields:
            save_click_setting(volume_percent=one(fields, "volume_percent"))
        else:
            save_click_setting(one(fields, "enabled") == "1")
        self.redirect_home("settings")

    def handle_spoken_navigation_setting_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        if "volume_percent" in fields:
            save_spoken_navigation_setting(volume_percent=one(fields, "volume_percent"))
        else:
            save_spoken_navigation_setting(one(fields, "enabled") == "1")
        self.redirect_home("settings")

    def handle_library_sort_setting_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        save_library_sort_setting(one(fields, "library_sort_mode"))
        self.redirect_home("settings", anchor="book-preferences")

    def handle_audio_test_post(self) -> None:
        output = AUDIO_TEST_OUTPUTS.get(one(self.post_fields() or {}, "output"))
        if output is None:
            self.send_request_error(400, "Choose the speaker or Bluetooth output.")
            return
        command = queue_web_player_command(AUDIO_TEST_COMMAND, output)
        self.send_response(303)
        self.send_header("Location", "/settings/audio-output?" + urlencode({"request": command.id}))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def handle_night_mode_post(self) -> None:
        mode = one(self.post_fields() or {}, "mode")
        if mode not in NIGHT_MODES:
            self.send_request_error(400, "Choose Automatic, On or Off.")
            return
        update_config(lambda current: replace(current, night_mode=mode))
        self.redirect_home("settings", anchor="night-mode")

    def handle_settings_preview_post(self) -> None:
        raw = one(self.post_fields() or {}, "backup")
        try:
            values = read_backup(raw)
        except BackupError as error:
            self.send_html(400, render_settings_restore(self.csrf_token(), notice=str(error)))
            return

        current = load_config()
        shelf = values.get("pinned_shelf")
        label = ""
        notice = ""
        if shelf and shelf != current.pinned_shelf:
            if current.is_ready:
                try:
                    label = next((option.label for option in available_shelves(current) if option.key == shelf), "")
                except AudiobookshelfError:
                    pass
            if not label:
                del values["pinned_shelf"]
                notice = "Pinned shelf skipped. Sign in to the same library and preview the backup again to restore it."
        raw = json.dumps({"version": BACKUP_VERSION, "settings": values})
        self.send_html(200, render_settings_restore(
            self.csrf_token(), values=values, current=current, raw=raw,
            ticket=preview_ticket(current, raw), shelf_label=label, notice=notice,
        ))

    def handle_settings_restore_post(self) -> None:
        fields = self.post_fields() or {}
        try:
            previous, updated = apply_preview(one(fields, "backup"), one(fields, "preview"))
        except BackupError as error:
            self.send_html(400, render_settings_restore(self.csrf_token(), notice=str(error)))
            return
        if previous.library_sort_mode != updated.library_sort_mode:
            queue_web_player_command("resort_library")
        if previous.pinned_shelf != updated.pinned_shelf:
            queue_web_player_command("reload_catalogue")
        self.send_html(200, render_settings_restore(
            self.csrf_token(), restored=True, notice="Your settings were restored.",
        ))

    def send_shelf_settings(self) -> None:
        config = load_config()
        if not config.is_ready:
            self.send_request_error(409, "Sign in to Audiobookshelf before choosing a shelf.")
            return
        try:
            shelves = available_shelves(config)
        except AudiobookshelfError:
            self.send_request_error(503, "Audiobookshelf shelves are unavailable. Try again from Settings.")
            return
        self.send_html(200, render_shelf_settings(shelves, config.pinned_shelf, self.csrf_token()))

    def send_catalogue_settings(self, request_id: str) -> None:
        config = load_config()
        self.send_html(200, render_catalogue_settings(
            config, load_catalogue_status(config), self.csrf_token(), request_id
        ))

    def send_catalogue_options(self) -> None:
        config = load_config()
        if not config.is_ready:
            self.send_json({"error": "Sign in to Audiobookshelf first."})
            return
        try:
            options = available_book_libraries(config)
        except (AudiobookshelfError, OSError, ValueError):
            self.send_json({"error": "Audiobookshelf libraries are unavailable. Check the connection and reload."})
            return
        self.send_json({"libraries": [{"id": option.id, "name": option.name} for option in options]})

    def send_catalogue_result(self, request_id: str) -> None:
        config = load_config()
        status = load_catalogue_status(config)
        self.send_json({
            "outcome": command_result(request_id),
            "lastRefresh": format_last_refresh(status),
            "error": status.error + "." if status.error else "",
            "libraryId": config.library_id,
            "libraryName": config.library_name,
            "catalogueKey": catalogue_request_key(config) if config.is_ready else "",
        })

    def handle_catalogue_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        config = load_config()
        if not config.is_ready:
            self.send_request_error(409, "Sign in to Audiobookshelf first.")
            return
        key = catalogue_request_key(config)
        if one(fields, "catalogue_key") != key:
            self.send_request_error(409, "The library settings changed. Reopen the library picker and try again.")
            return
        action = one(fields, "action")
        library_id = one(fields, "library_id") if action == "select" else ""
        if action not in ("select", "refresh") or (action == "select" and not 0 < len(library_id) <= 200):
            self.send_request_error(400, "Choose an audiobook library.")
            return
        command = queue_web_player_command(CATALOGUE_COMMAND, catalogue_key=key, library_id=library_id)
        self.send_response(303)
        self.send_header("Location", "/settings/catalogue?" + urlencode({"request": command.id}))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_library(self, fields: dict[str, list[str]], *, guest_sections: tuple[str, ...] | None = None) -> None:
        config = load_config()
        view = one(fields, "view")
        view = view if view in BROWSE_VIEWS else "titles"
        group = one(fields, "group")[:240]
        group_view = one(fields, "group_view")
        if group_view and group_view != view:
            group = ""
        downloaded_only = one(fields, "downloaded") == "1"
        filters = library_display_filters(fields)
        books = load_cached_books(config)
        catalogue_total = len(books)
        pending = False
        notice = ""
        if not books and config.is_ready and not downloaded_only and view != "podcasts":
            state = web_catalogue.request(config)
            pending = state == "loading"
            if pending:
                notice = "Loading library from Audiobookshelf…"
            elif state == "save_failed":
                notice = "Could not save the catalogue on the player. Check its available storage and try again."
            elif state == "failed":
                notice = "Could not load the library. Check the Audiobookshelf connection."

        copies = web_downloads(config)
        shelves = None
        if view == "podcasts" and not downloaded_only:
            episodes, waiting = web_reads.get(library_read_key(config, view), lambda: load_podcast_catalogue(config))
            books = [replace(book) for book in episodes] if episodes is not None else []
            pending = pending or waiting
            if episodes is None:
                notice = "Loading podcasts…" if waiting else "Podcasts are unavailable."
        else:
            books = books_with_downloads(config, books, include_incomplete=True, copies=copies)
        if view in ("collections", "playlists") and config.is_ready:
            rows, waiting = web_reads.get(library_read_key(config, view), lambda: browser_shelves(config, view))
            shelves = shelf_groups(config, view, books, rows=rows or [])
            pending = pending or waiting
            if rows is None:
                notice = "Loading shelves…" if waiting else "Audiobookshelf shelves are unavailable."
        if books:
            account = progress_account(config)
            updates = read_listening_progress() if account else {}
            for book in books:
                update = updates.get((account, book.id, ""))
                if update and update.updated_at >= book.progress_updated_at:
                    book.current_time = update.current_time
                    book.progress_updated_at = update.updated_at
                    book.progress = min(1, update.current_time / update.duration) if update.duration else 0
                    book.is_finished = book.progress >= .995
        can_manage = guest_sections is None
        self.send_html(200, render_library(
            config, books, one(fields, "q")[:200], downloaded_only, library_page(fields),
            self.csrf_token() if can_manage else "",
            view=view, group=group, shelves=shelves, notice=notice, copies=copies,
            catalogue_total=catalogue_total, catalogue_pending=pending,
            player=load_web_player_status() if can_manage else None, guest_sections=guest_sections,
            space_remaining=download_space.reading(config).remaining if filters["section"] == "downloads" else None,
            fragment=getattr(self, "headers", {}).get("X-Chapter-Fragment") == "results", **filters,
        ))

    def send_book_details(self, fields: dict[str, list[str]], *, guest_sections: tuple[str, ...] | None = None) -> None:
        config = load_config()
        item_id = one(fields, "id")
        directory = book_directory(config, item_id)
        record = read_download(directory) if config.is_ready and item_id else None
        book = record.book if record else find_library_book(config, item_id)
        if book is None:
            self.send_request_error(404, "This book is not in the current library.")
            return
        metadata = read_details(directory) if record else None
        pending = False
        if metadata is None and not (record and record.complete):
            metadata, pending = web_reads.get(
                ("details", progress_account(config), item_id), lambda: book_metadata(config, book),
            )
        self.send_html(200, render_book_details(
            book, metadata or {}, pending=pending,
            fragment=getattr(self, "headers", {}).get("X-Chapter-Fragment") == "details",
            guest_sections=guest_sections,
            section="downloads" if one(fields, "section") == "downloads" else "library",
            csrf=self.csrf_token() if guest_sections is None else "",
        ))

    def handle_library_download_settings(self) -> None:
        change_download_settings(self)

    def handle_library_download(self) -> None:
        change_download(self, download_space)

    def send_download_status(self, fields: dict[str, list[str]], *, can_manage: bool = True) -> None:
        config = load_config()
        csrf = self.csrf_token() if can_manage else None
        payload = download_status_payload(config, fields, csrf=csrf, space=download_space)
        if one(fields, "catalogue_pending") == "1":
            pending_lookup = web_reads.pending(library_read_key(config, one(fields, "view")))
            payload["cataloguePending"] = web_catalogue.pending(config) or pending_lookup
        self.send_json(payload)

    def redirect_library(self, fields: dict[str, list[str]]) -> None:
        self.send_response(303)
        self.send_header("Location", library_url(one(fields, "q")[:200], one(fields, "downloaded") == "1",
                                                 library_page(fields), view=one(fields, "view") or "titles",
                                                 group=one(fields, "group")[:240], **library_display_filters(fields)))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def handle_shelf_setting_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        shelf = one(fields, "shelf")
        config = load_config()
        try:
            if shelf and (not config.is_ready or shelf not in {row.key for row in available_shelves(config)}):
                self.send_request_error(400, "Choose an accessible collection or playlist.")
                return
        except AudiobookshelfError:
            self.send_request_error(503, "Audiobookshelf shelves are unavailable. Try again from Settings.")
            return
        def pin(current: AppConfig) -> AppConfig:
            if catalogue_request_key(current) != catalogue_request_key(config):
                raise ValueError("The selected library changed")
            return replace(current, pinned_shelf=shelf)

        try:
            update_config(pin)
        except ValueError:
            self.send_request_error(409, "The library changed. Choose the shelf again.")
            return
        queue_web_player_command("reload_catalogue")
        self.redirect_home("settings", anchor="book-preferences")

    def handle_playback_speed_setting_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        save_playback_speed_setting(one(fields, "playback_speed"))
        self.redirect_home("settings", anchor="book-preferences")

    def handle_screen_saver_setting_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        save_screen_saver_setting(
            one(fields, "screen_saver_mode"),
            one(fields, "screen_saver_dim_percent") if "screen_saver_dim_percent" in fields else None,
            clock_format=(
                one(fields, "screen_saver_clock_format")
                if "screen_saver_clock_format" in fields
                else None
            ),
        )
        self.redirect_home("settings", anchor="screen-saver")

    def handle_sleep_setting_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        if "enabled" in fields:
            save_sleep_timer_setting(enabled=one(fields, "enabled") == "1")
            self.redirect_home("settings", anchor="night-mode")
            return
        try:
            minutes = int(one(fields, "minutes"))
        except ValueError:
            self.send_page(message="Sleep timer must be a whole number of minutes.", is_error=True, active_tab="settings")
            return
        save_sleep_timer_setting(minutes=minutes)
        self.redirect_home("settings", anchor="night-mode")

    def handle_listening_setting_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        mode = one(fields, "night_mode")
        consolidated = one(fields, "listening_page") == "1"
        if consolidated and mode not in NIGHT_MODES:
            self.send_request_error(400, "Choose Automatic, On or Off.")
            return
        update_config(lambda config: replace(
            config,
            night_mode=mode if consolidated else config.night_mode,
            sleep_timer_enabled=one(fields, "sleep_timer_enabled") == "1" if consolidated else config.sleep_timer_enabled,
            sleep_timer_minutes=(
                valid_sleep_timer_minutes(one(fields, "sleep_timer_minutes")) if consolidated else config.sleep_timer_minutes
            ),
            skip_back_seconds=valid_skip_seconds(one(fields, "skip_back_seconds"), config.skip_back_seconds),
            skip_forward_seconds=valid_skip_seconds(one(fields, "skip_forward_seconds"), config.skip_forward_seconds),
            night_start=valid_clock_time(one(fields, "night_start")),
            night_end=valid_clock_time(one(fields, "night_end")),
            night_dim_percent=max(1, valid_feedback_volume(one(fields, "night_dim_percent"), config.night_dim_percent)),
            night_volume_limit=max(1, valid_feedback_volume(one(fields, "night_volume_limit"), config.night_volume_limit)),
            sleep_rewind=one(fields, "sleep_rewind") == "1",
        ))
        if consolidated:
            self.redirect_settings("listening")
        else:
            self.redirect_home("settings", anchor="night-mode")

    def handle_podcasts_post(self) -> None:
        fields = self.post_fields()
        if fields is None:
            return
        urls = fields.get("podcast_url", [])
        try:
            save_podcast_settings(urls)
        except PodcastError as error:
            self.send_page(
                message=f"Podcast save failed: {html.escape(str(error))}", is_error=True, active_tab="podcasts",
            )
            return
        self.redirect_home("podcasts")

    def handle_system_reboot_post(self) -> None:
        self.send_page(
            message="Rebooting the Chapter player. It will be available again shortly.", active_tab="system",
        )
        schedule_system_reboot()

    def handle_system_update_post(self) -> None:
        if os.environ.get("CHAPTER_RELEASE_SOURCE") == "local":
            self.send_request_error(409, "This release is installed locally. Use the local release installer.")
            return
        with update_request_lock:
            if update_state_is_active(load_update_state()):
                self.send_request_error(409, "A software update is already running.")
                return
            version = one(self.post_fields() or {}, "version").strip()
            if not version_tuple(version) or version.startswith("v"):
                self.send_request_error(400, "Review the available release before installing it.")
                return
            save_update_state(SoftwareUpdateState(
                phase="installing", target_version=version,
                message=f"Preparing to install v{version}.", updated_at=time.time(),
            ))
        queue_web_player_command("pause")
        self.send_page(
            message=(
                f"Software update requested for v{version}. Chapter will pause playback and restart its services."
            ),
            active_tab="system",
        )
        schedule_software_update()

    def handle_system_reset_post(self) -> None:
        restore_device_defaults()
        self.send_page(
            message=(
                f"Default settings restored. In about 5 seconds, connect to "
                f"{html.escape(SETUP_HOTSPOT_SSID)}, then browse to {html.escape(SETUP_HOTSPOT_URL)}."
            ),
            active_tab="system",
        )
        schedule_system_reset()

    def send_page(
        self,
        message: str = "",
        is_error: bool = False,
        server_url: str | None = None,
        active_tab: str = "",
    ) -> None:
        config = load_config()
        body = render_setup_page(
            config=config,
            wifi=web_wifi.current(wifi_status),
            abs_status=connection_monitor.status_for(config),
            player_status=load_web_player_status(),
            bt_status=web_bluetooth.current(bluetooth_status),
            csrf_token=self.csrf_token(),
            message=message,
            is_error=is_error,
            server_url=server_url,
            active_tab=active_tab,
            download_activity=download_activity_status(config),
        )
        self.send_html(200, body)

    def send_csrf_error(self) -> None:
        self.send_request_error(
            403,
            "This page has expired. Reload the Chapter setup page, then try again.",
        )

    def redirect_settings(self, section: str) -> None:
        self.send_response(303)
        self.send_header("Location", f"/settings/{section}?saved=1")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def redirect_home(self, tab: str = "", *, anchor: str = "") -> None:
        location = "/?" + urlencode({"tab": tab}) if tab else "/"
        if anchor:
            location += "#" + quote(anchor, safe="")
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_player_command_response(self, command_id: str) -> None:
        if self.headers.get("X-Requested-With") == "fetch":
            self.send_json({"id": command_id, "outcome": "queued"}, status=202)
            return
        self.redirect_home()

    def send_player_status_json(self) -> None:
        self.send_json(player_status_payload(load_web_player_status()))

    def send_system_update_status_json(self) -> None:
        if os.environ.get("CHAPTER_RELEASE_SOURCE") == "local":
            self.send_json({"phase": "local", "updateAvailable": False, "message": "Installed from a local package."})
            return
        self.send_json(update_status_payload())

    def send_player_events(self) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        stream_player_events(self.wfile, load_web_player_status, self.owner_authorized)

    def send_health_page(self) -> None:
        wifi = web_wifi.current(wifi_status)
        abs_status = connection_monitor.status_for(load_config())
        self.send_html(200, render_health_page(wifi, abs_status))

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def library_read_key(config: AppConfig, view: str) -> tuple[str, str]:
    return view, catalogue_request_key(config)


def schedule_wifi_connect(ssid: str, password: str) -> None:
    def worker() -> None:
        try:
            print(f"Wi-Fi connection starting for {ssid}", flush=True)
            connect_wifi(ssid, password)
            print(f"Wi-Fi connection complete for {ssid}", flush=True)
        except WifiError as error:
            print(f"Wi-Fi connection failed: {error}", flush=True)

    timer = threading.Timer(WIFI_CHANGE_DELAY_SECONDS, worker)
    timer.daemon = True
    timer.start()


def schedule_wifi_forget(delay_seconds: float = WIFI_CHANGE_DELAY_SECONDS) -> None:
    def worker() -> None:
        try:
            print(f"Wi-Fi forget starting; switching to {SETUP_HOTSPOT_SSID}", flush=True)
            forget_current_wifi_and_start_hotspot()
            print(f"Wi-Fi forget complete; {SETUP_HOTSPOT_SSID} should be active", flush=True)
        except WifiError as error:
            print(f"Wi-Fi forget failed: {error}", flush=True)

    timer = threading.Timer(delay_seconds, worker)
    timer.daemon = True
    timer.start()


SystemCommandRunner = Callable[[list[str]], subprocess.CompletedProcess]
UpdateDisplayReadyWaiter = Callable[[], bool]


def run_system_command(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, check=True, text=True)


def schedule_system_reboot(
    delay_seconds: float = SYSTEM_ACTION_DELAY_SECONDS,
    runner: SystemCommandRunner = run_system_command,
) -> None:
    def worker() -> None:
        try:
            print("System reboot starting", flush=True)
            runner(["systemctl", "reboot"])
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"System reboot failed: {error}", flush=True)

    timer = threading.Timer(delay_seconds, worker)
    timer.daemon = True
    timer.start()


def schedule_software_update(
    delay_seconds: float = SYSTEM_ACTION_DELAY_SECONDS,
    runner: SystemCommandRunner = run_system_command,
    display_ready_waiter: UpdateDisplayReadyWaiter = wait_for_update_display_ready,
) -> None:
    def worker() -> None:
        try:
            if not display_ready_waiter():
                print("Software update display acknowledgement timed out; continuing", flush=True)
            print("Software update starting", flush=True)
            runner(["systemctl", "start", "--no-block", UPDATE_SERVICE_NAME])
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"Software update failed to start: {error}", flush=True)
            save_update_state(
                SoftwareUpdateState(
                    phase="failed",
                    message="Software update could not start. Please try again.",
                    updated_at=time.time(),
                )
            )

    timer = threading.Timer(delay_seconds, worker)
    timer.daemon = True
    timer.start()


def restore_device_defaults() -> AppConfig:
    config = AppConfig()
    save_config(config)
    downloads.configuration_changed(config)
    clear_cached_books()
    clear_last_playback()
    queue_web_player_command("refresh_library")
    return config


def schedule_system_reset(delay_seconds: float = WIFI_CHANGE_DELAY_SECONDS) -> None:
    def worker() -> None:
        try:
            print(f"System reset starting; switching to {SETUP_HOTSPOT_SSID}", flush=True)
            forget_all_wifi_connections_and_start_hotspot()
            print(f"System reset complete; {SETUP_HOTSPOT_SSID} should be active", flush=True)
        except WifiError as error:
            print(f"System reset Wi-Fi change failed: {error}", flush=True)

    timer = threading.Timer(delay_seconds, worker)
    timer.daemon = True
    timer.start()


def login_and_refresh_player(server_url: str, username: str, password: str) -> AppConfig:
    existing_config = load_config()
    client, login_data = AudiobookshelfClient.login(server_url, username, password)
    default_library = str(login_data.get("userDefaultLibraryId") or "")
    if not default_library and not (same_login(existing_config, server_url, username) and existing_config.library_id):
        default_library = client.choose_library_id()

    resolved_library = existing_config.library_id if same_login(existing_config, server_url, username) else ""

    def apply_login(current: AppConfig) -> AppConfig:
        same_account = same_login(current, server_url, username)
        return replace(
            current,
            server_url=server_url.strip(),
            token=client.token,
            refresh_token=getattr(client, "refresh_token", ""),
            library_id=(current.library_id if same_account else "") or default_library or resolved_library,
            library_name=current.library_name if same_account else "",
            username=login_username(login_data, username),
            pinned_shelf=current.pinned_shelf if same_account else "",
        )

    _, config = update_config(apply_login)
    downloads.configuration_changed(config)
    queue_web_player_command("pause")
    queue_web_player_command("refresh_library")
    return config


def save_library_sort_setting(mode: str) -> AppConfig:
    _, updated = update_config(lambda config: replace(config, library_sort_mode=valid_library_sort_mode(mode)))
    queue_web_player_command("resort_library")
    return updated


def save_podcast_settings(urls: list[str]) -> AppConfig:
    podcasts = [resolve_podcast_config(url) for url in normalized_podcast_urls(urls)]
    _, updated = update_config(lambda config: replace(config, podcasts=podcasts))
    queue_web_player_command("refresh_library")
    return updated


def normalized_podcast_urls(urls: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for url in urls:
        clean_url = url.strip()
        if not clean_url:
            continue
        key = clean_url.lower()
        if key in seen:
            continue
        normalized.append(clean_url)
        seen.add(key)
    return normalized


def bluetooth_status() -> tuple[bool, str]:
    device = connected_bluetooth_audio_device()
    if device is None:
        return False, "Not connected"
    return True, f"Connected to {device.name}"


def login_username(login_data: dict, fallback: str) -> str:
    user = login_data.get("user") or {}
    candidates = [
        user.get("username"),
        user.get("name"),
        user.get("displayName"),
        login_data.get("username"),
        login_data.get("name"),
        login_data.get("displayName"),
        fallback,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


class SetupServer(ThreadingHTTPServer):
    request_queue_size = 64

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(15)
        return request, address


def run_setup_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = SetupServer((host, port), SetupHandler)
    server.csrf_token = secrets.token_urlsafe(32)
    log_installed_build("setup")
    print(f"Setup page listening on http://{host}:{port}")
    downloads.start_worker()
    try:
        server.serve_forever()
    finally:
        downloads.close()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Chapter Player for Audiobookshelf setup page.")
    parser.add_argument(
        "--ensure-wifi",
        action="store_true",
        help="Start the setup hotspot if no Wi-Fi connection is active.",
    )
    args = parser.parse_args()
    if args.ensure_wifi:
        try:
            print(ensure_wifi_or_hotspot())
        except WifiError as error:
            print(f"Wi-Fi setup mode could not start: {error}")
    run_setup_server()


if __name__ == "__main__":
    main()
