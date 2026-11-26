"""Render the admin pages from settings and current device status."""

from __future__ import annotations

import html
from datetime import datetime

from . import __version__
from .admin_layout import (
    ASSET_DIR,
    DASHBOARD_TABS,
    TEMPLATE_DIR,
    cover_icon,
    render_admin_page,
    render_cover,
)
from .bedtime import night_mode_active
from .config import (
    CLOCK_FORMAT_12_HOUR,
    CLOCK_FORMAT_24_HOUR,
    LIBRARY_SORT_AUTHOR,
    LIBRARY_SORT_AUTHOR_FIRST,
    LIBRARY_SORT_TITLE,
    MAX_SLEEP_TIMER_MINUTES,
    MIN_SLEEP_TIMER_MINUTES,
    PLAYBACK_SPEEDS,
    SCREEN_OFF_DELAYS,
    SCREEN_SAVER_BOOKS,
    SCREEN_SAVER_CHAPTER,
    SCREEN_SAVER_CLOCK,
    SCREEN_SAVER_DIM_LEVELS,
    AppConfig,
    PodcastConfig,
)
from .connection_health import ConnectionHealth, ConnectionState
from .download_transfer import MIB, storage_usage
from .library import ShelfOption
from .listening_status import listening_source
from .player_control import WebPlayerStatus
from .screen_saver import clock_screen_saver_text
from .wifi import SETUP_HOTSPOT_SSID, SETUP_HOTSPOT_URL, WifiStatus


CSRF_FIELD_NAME = "csrf_token"
PAGE = (TEMPLATE_DIR / "setup.html").read_text(encoding="utf-8")
HEALTH_PAGE = (TEMPLATE_DIR / "health.html").read_text(encoding="utf-8")
SETTINGS_INDEX = (TEMPLATE_DIR / "settings_index.html").read_text(encoding="utf-8")
SETTINGS_PAGES = {
    "sound": "Sound and feedback", "display": "Display",
    "listening": "Listening and Night mode", "books": "Books", "downloads": "Downloads",
}
SETTINGS_TEMPLATES = {
    section: (TEMPLATE_DIR / f"settings_{section}.html").read_text(encoding="utf-8")
    for section in SETTINGS_PAGES
}
SETUP_STYLES = (ASSET_DIR / "setup.css").read_text(encoding="utf-8")
SETUP_SCRIPT = (ASSET_DIR / "setup.js").read_text(encoding="utf-8")
NAVIGATION_SCRIPT = (ASSET_DIR / "setup_navigation.js").read_text(encoding="utf-8")
CONNECTION_HEALTH_SCRIPT = (ASSET_DIR / "connection_health.js").read_text(encoding="utf-8")


def render_setup_page(
    *,
    config: AppConfig,
    wifi: WifiStatus | None,
    abs_status: ConnectionHealth,
    player_status: WebPlayerStatus,
    bt_status: tuple[bool, str],
    csrf_token: str,
    message: str = "",
    is_error: bool = False,
    server_url: str | None = None,
    active_tab: str = "",
    download_activity: dict[str, str] | None = None,
) -> str:
    message_html = ""
    if message:
        css_class = "message error" if is_error else "message"
        role = "alert" if is_error else "status"
        message_html = f'<div class="{css_class}" role="{role}">{message}</div>'

    section = active_tab if active_tab in DASHBOARD_TABS else "overview"
    hidden_panels = {f"{name}_hidden": "" if name == section else " hidden" for name in DASHBOARD_TABS}
    content = PAGE.format(
        restore_tab="0" if active_tab else "1",
        status_panel=render_status_panel(
            wifi, abs_status, player_status, bt_status, csrf_token, download_activity,
            night_mode_status=format_night_mode_status(config),
        ),
        settings_card=SETTINGS_INDEX,
        podcasts_card=render_podcasts_card(config, csrf_token),
        wifi_card=render_wifi_card(wifi, csrf_token),
        system_card=render_system_card(csrf_token),
        message=message_html,
        server_url=html.escape(server_url if server_url is not None else config.server_url),
        csrf_input=render_csrf_input(csrf_token),
        **hidden_panels,
    )
    scripts = "\n".join(
        f"<script>{source}</script>"
        for source in (
            NAVIGATION_SCRIPT,
            SETUP_SCRIPT.replace("$csrf_field_name", CSRF_FIELD_NAME),
            CONNECTION_HEALTH_SCRIPT,
        )
    )
    return render_admin_page(
        "Chapter Player", section, content, styles=SETUP_STYLES, scripts=scripts, csrf_token=csrf_token,
    )


def render_csrf_input(csrf_token: str) -> str:
    return (
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" '
        f'value="{html.escape(csrf_token, quote=True)}">'
    )


def render_health_page(wifi: WifiStatus | None, abs_status: ConnectionHealth) -> str:
    content = HEALTH_PAGE.format(
        wifi_class=wifi_status_class(wifi),
        wifi_value=render_wifi_status_value(wifi),
        abs_value=render_connection_health(abs_status),
        version=html.escape(__version__),
    )
    return render_admin_page(
        "Player health · Chapter", "", content,
        styles=SETUP_STYLES, scripts=f"<script>{CONNECTION_HEALTH_SCRIPT}</script>", parent=("Overview", "/?tab=overview"),
    )


def render_request_error(message: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Chapter Player</title></head><body><main>"
        f"<h1>Chapter Player</h1><p>{html.escape(message)}</p>"
        '<p><a href="/">Reload setup page</a></p></main></body></html>'
    )


def render_shelf_settings(shelves: list[ShelfOption], selected: str, csrf_token: str) -> str:
    options = ['<option value="">No pinned shelf</option>']
    for shelf in shelves:
        selection = " selected" if shelf.key == selected else ""
        options.append(f'<option value="{html.escape(shelf.key)}"{selection}>{html.escape(shelf.label)}</option>')
    content = (
        '<header class="page-heading"><h1>Pinned library shelf</h1></header>'
        '<section class="divider-section"><form class="field-action short-form" method="post" action="/settings/shelves">'
        f'{render_csrf_input(csrf_token)}<label class="field">Collection or playlist <select name="shelf">{"".join(options)}'
        '</select></label> <button type="submit">Save</button></form></section>'
    )
    return render_admin_page("Pinned library shelf · Chapter", "settings", content, parent=("Books", "/settings/books"))


def format_download_summary(downloaded: int, downloading: int) -> str:
    book_label = "book" if downloaded == 1 else "books"
    label = f"{downloaded:,} {book_label} downloaded"
    return f"{label} · {downloading:,} downloading" if downloading else label


def format_download_activity(downloaded: int, downloading: int, queued: int, space: str) -> dict[str, str]:
    return {
        "label": format_download_summary(downloaded, downloading),
        "downloaded": format_download_summary(downloaded, 0),
        "downloading": f"{downloading:,} downloading" if downloading else "",
        "queued": f"{queued:,} in queue",
        "space": space,
    }


def format_night_mode_status(config: AppConfig, now: datetime | None = None) -> str:
    mode, start, end = config.night_mode, config.night_start, config.night_end
    if mode == "on":
        return "Active · No scheduled end"
    if mode == "off":
        return "Off"
    if not start or not end or start == end:
        return "Off · Schedule not set"

    current = now if now is not None else datetime.now()
    active = night_mode_active(mode, start, end, current)
    hour, minute = map(int, (end if active else start).split(":"))
    change = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    day = " tomorrow" if change <= current else ""
    clock = clock_screen_saver_text(change, clock_format=config.screen_saver_clock_format)
    label = "Active · Ends at" if active else "Off · Starts at"
    return f"{label} {clock}{day}"


def render_status_panel(
    wifi: WifiStatus | None,
    abs_status: ConnectionHealth,
    player_status: WebPlayerStatus | None = None,
    bt_status: tuple[bool, str] = (False, "Not connected"),
    csrf_token: str = "",
    download_activity: dict[str, str] | None = None,
    *,
    night_mode_status: str = "Off · Schedule not set",
) -> str:
    software = f"""
    <div class="status-row">
      <dt class="status-label">Software</dt>
      <dd class="status-value">v{html.escape(__version__)} · <span data-update-summary>Checking for updates…</span></dd>
    </div>
    """
    return render_overview_content(
        render_player_status_value(player_status or WebPlayerStatus(), csrf_token), night_mode_status,
        download_activity, software=software, connections=render_overview_connections(wifi, abs_status, bt_status),
    )


def render_guest_overview(
    config: AppConfig, player_status: WebPlayerStatus, download_activity: dict[str, str], guest_sections: tuple[str, ...],
) -> str:
    listening = overview_listening_status(player_status)
    player = (
        f'<h3 data-overview-value="playingTitle">{html.escape(listening["playingTitle"])}</h3>'
        f'<p class="now-playing-meta muted" data-overview-value="playingMeta">{html.escape(listening["playingMeta"])}</p>'
    )
    return render_admin_page(
        "Overview · Chapter", "overview", '<header class="page-heading"><h1>Overview</h1></header>'
        + render_overview_content(player, format_night_mode_status(config), download_activity),
        styles=SETUP_STYLES, scripts=f"<script>{CONNECTION_HEALTH_SCRIPT}</script>", guest_sections=guest_sections,
    )


def overview_listening_status(status: WebPlayerStatus) -> dict[str, str]:
    return {
        "playingTitle": status.title if status.has_session else "Nothing playing",
        "playingMeta": f"{player_state_label(status)} · {render_time_info(status)}" if status.has_session else "",
    }


def render_overview_content(
    player: str, night_mode_status: str, download_activity: dict[str, str] | None,
    *, software: str = "", connections: str = "",
) -> str:
    activity = download_activity or format_download_activity(0, 0, 0, "Checking space for downloads…")
    return """
    <div class="overview-grid">
      <section class="download-activity" aria-label="Download activity">
        <span data-overview-value="downloaded">{downloaded}</span>
        <span data-overview-value="downloading"{idle}>{downloading}</span>
        <span data-overview-value="queued">{queued}</span>
        <span data-overview-value="space">{space}</span>
      </section>
      <div class="divider-stack">
        <section class="divider-section" aria-labelledby="now-playing-heading">
          <h2 id="now-playing-heading">Now playing</h2>
          {player}
        </section>
        <section class="divider-section" aria-labelledby="player-status-heading">
          <h2 id="player-status-heading">Player status</h2>
          <dl class="status row-dividers">
            <div class="status-row">
              <dt class="status-label">Night mode</dt>
              <dd class="status-value" data-overview-value="nightMode" aria-live="polite">{night_mode_status}</dd>
            </div>
            {software}
          </dl>
        </section>
        {connections}
      </div>
    </div>
    """.format(
        idle="" if activity["downloading"] else " hidden",
        night_mode_status=html.escape(night_mode_status),
        player=player, software=software, connections=connections,
        **{key: html.escape(label) for key, label in activity.items()},
    )


def render_overview_connections(wifi: WifiStatus | None, abs_status: ConnectionHealth, bt_status: tuple[bool, str]) -> str:
    bt_ok, bt_message = bt_status
    return """
    <section class="divider-section" aria-labelledby="status-heading">
      <h2 id="status-heading">Connections</h2>
      <dl class="status row-dividers">
        <div class="status-row">
          <dt class="status-label">Wi-Fi</dt>
          <dd class="status-value {wifi_class}" data-wifi-status>{wifi_value}</dd>
        </div>
        <div class="status-row">
          <dt class="status-label">Audiobookshelf</dt>
          <dd class="status-value"><span data-connection-health role="status">{abs_value}</span></dd>
        </div>
        <div class="status-row">
          <dt class="status-label">Bluetooth</dt>
          <dd class="status-value {bt_class}"><span data-bluetooth-status>{bt_value}</span></dd>
        </div>
      </dl>
    </section>
    """.format(
        wifi_class=wifi_status_class(wifi),
        wifi_value=render_wifi_status_value(wifi),
        abs_value=render_connection_health(abs_status),
        bt_class="ok" if bt_ok else "",
        bt_value=render_bluetooth_status_value(bt_message),
    )


def render_system_card(csrf_token: str = "") -> str:
    return """
    <div class="divider-stack">
    <section class="divider-section" aria-labelledby="system-heading">
      <h2 id="system-heading">Software and recovery</h2>
      <div class="system-version">
        <div>
          <div class="system-version-line">
            <span class="muted">Software version</span>
            <strong>v{version}</strong>
          </div>
          <span class="system-update-status" data-system-update-status aria-live="polite">Checking for updates...</span>
          <p class="system-update-description muted" data-system-update-description></p>
        </div>
        <button type="button" aria-expanded="false" aria-controls="confirm-update" data-confirm-trigger="update" data-system-update-trigger hidden>Update</button>
        <div class="confirmation-panel update-confirmation" id="confirm-update" data-confirm-panel="update" hidden>
          <strong>Install <span data-system-update-version>the latest release</span>?</strong>
          <p>Playback will pause while Chapter updates and checks both services. If either check fails, Chapter restores the previous build.</p>
          <div class="confirmation-actions">
            <button class="secondary" type="button" data-confirm-cancel>Cancel</button>
            <form method="post" action="/system/update">
              {csrf_input}
              <input type="hidden" name="version" data-system-update-target>
              <button type="submit">Install Update</button>
            </form>
          </div>
        </div>
      </div>
    </section>
    <section class="divider-section" aria-labelledby="system-tools-heading">
      <h2 id="system-tools-heading">Diagnostics and settings backup</h2>
      <nav class="resource-list row-dividers" aria-labelledby="system-tools-heading">
        <a class="resource-link" href="/settings/security">
          <span><strong>Security</strong><small>Manage owner login and visitor access</small></span>
        </a>
        <a class="resource-link" href="/system/status">
          <span><strong>System status and diagnostics</strong><small>Check device health, storage and recent startup timings</small></span>
        </a>
        <a class="resource-link download-link" href="/system/settings-backup" download>
          <span><strong>Download settings backup</strong><small>Save a copy of your preferences to this device</small></span>
        </a>
        <a class="resource-link" href="/system/settings-restore">
          <span><strong>Restore settings</strong><small>Preview a backup before applying it</small></span>
        </a>
      </nav>
    </section>
    <section class="divider-section" aria-labelledby="device-actions-heading">
      <h2 id="device-actions-heading">Device actions</h2>
      <div class="system-actions row-dividers">
        <div class="system-action">
          <div>
            <h3>Reboot</h3>
            <p class="muted">Restart the Chapter player without changing its settings.</p>
          </div>
          <button class="secondary" type="button" aria-expanded="false" aria-controls="confirm-reboot" data-confirm-trigger="reboot">Reboot</button>
          <div class="confirmation-panel" id="confirm-reboot" data-confirm-panel="reboot" hidden>
            <strong>Are you sure you want to reboot?</strong>
            <p>Playback will stop while the device restarts.</p>
            <div class="confirmation-actions">
              <button class="secondary" type="button" data-confirm-cancel>Cancel</button>
              <form method="post" action="/system/reboot">
                {csrf_input}
                <button type="submit">Reboot</button>
              </form>
            </div>
          </div>
        </div>
        <div class="system-action">
          <div>
            <h3>Restore Device to Default Settings</h3>
            <p class="muted">Clear this player and return it to Wi-Fi setup mode.</p>
          </div>
          <button class="danger-button" type="button" aria-expanded="false" aria-controls="confirm-restore" data-confirm-trigger="restore">Factory reset</button>
          <div class="confirmation-panel" id="confirm-restore" data-confirm-panel="restore" hidden>
            <strong>Are you sure you want to restore the device to default settings?</strong>
            <p>This clears the Audiobookshelf login, listening state, saved podcasts, and all saved Wi-Fi networks, and restores player settings to defaults. This cannot be undone.</p>
            <div class="confirmation-actions">
              <button class="secondary" type="button" data-confirm-cancel>Cancel</button>
              <form method="post" action="/system/reset">
                {csrf_input}
                <button class="danger-button" type="submit">Restore Device</button>
              </form>
            </div>
          </div>
        </div>
      </div>
    </section>
    </div>
    """.format(version=html.escape(__version__), csrf_input=render_csrf_input(csrf_token))


def render_settings_card(config: AppConfig, csrf_token: str = "", *, section: str) -> str:
    if section == "downloads":
        used, free = storage_usage()
        return SETTINGS_TEMPLATES[section].format(
            used=used / MIB, free=free / MIB, limit=config.download_limit_mib,
            prefer_downloaded=" checked" if config.prefer_downloaded else "",
            csrf=render_csrf_input(csrf_token),
        )
    return SETTINGS_TEMPLATES[section].format(
        click_checked=" checked" if config.control_click_enabled else "",
        click_volume=config.control_click_volume_percent,
        speech_checked=" checked" if config.spoken_navigation_enabled else "",
        speech_volume=config.spoken_navigation_volume_percent,
        library_sort_options=render_library_sort_options(config.library_sort_mode),
        library_name=f'<small>{html.escape(config.library_name)}</small>' if config.library_name else "",
        playback_speed_options=render_playback_speed_options(config.playback_speed),
        screen_saver_options=render_screen_saver_options(config.screen_saver_mode),
        screen_saver_dim_options=render_screen_saver_dim_options(config.screen_saver_dim_percent),
        screen_saver_clock_options=render_screen_saver_clock_options(config.screen_saver_clock_format),
        screen_off_options="".join(
            f'<option value="{minutes}"{" selected" if minutes == config.screen_off_minutes else ""}>'
            f'{str(minutes) + " min" if minutes else "Never"}</option>' for minutes in SCREEN_OFF_DELAYS
        ),
        screen_off_start=config.screen_off_start,
        screen_off_end=config.screen_off_end,
        sleep_checked=" checked" if config.sleep_timer_enabled else "",
        sleep_minutes=config.sleep_timer_minutes,
        sleep_min=MIN_SLEEP_TIMER_MINUTES,
        sleep_max=MAX_SLEEP_TIMER_MINUTES,
        skip_back_seconds=config.skip_back_seconds,
        skip_forward_seconds=config.skip_forward_seconds,
        sleep_rewind_checked=" checked" if config.sleep_rewind else "",
        night_mode_options="".join(
            f'<option value="{mode}"{" selected" if config.night_mode == mode else ""}>{label}</option>'
            for mode, label in (("schedule", "Automatic"), ("on", "On"), ("off", "Off"))
        ),
        night_start=config.night_start,
        night_end=config.night_end,
        night_dim_percent=config.night_dim_percent,
        night_volume_limit=config.night_volume_limit,
        csrf=render_csrf_input(csrf_token),
    )


def render_settings_page(config: AppConfig, section: str, csrf_token: str, *, saved: bool = False, error: str = "") -> str:
    title = SETTINGS_PAGES[section]
    message = '<p class="message" role="status">Settings saved.</p>' if saved else ""
    if error:
        message = f'<p class="message error" role="alert">{html.escape(error)}</p>'
    content = (
        f'<header class="page-heading"><h1>{title}</h1></header>{message}'
        + render_settings_card(config, csrf_token, section=section)
    )
    return render_admin_page(
        f"{title} · Chapter", "settings", content, styles=SETUP_STYLES, parent=("All settings", "/?tab=settings"),
    )


def render_podcasts_card(config: AppConfig, csrf_token: str = "") -> str:
    rows = [render_podcast_row(index, podcast) for index, podcast in enumerate(config.podcasts)]
    rows.append(render_podcast_row(len(rows), None))
    return """
    <section class="divider-section" aria-labelledby="podcasts-heading">
      <h2 id="podcasts-heading">Podcast feeds</h2>
      <p class="hint">Paste the show link from podcasts.apple.com or enter an RSS feed URL.</p>
      <form class="podcast-form" method="post" action="/podcasts">
        {csrf_input}
        <div class="podcast-table row-dividers" data-podcast-rows>
          {rows}
        </div>
        <div class="form-footer podcast-actions">
          <button class="small-button secondary" type="button" data-add-podcast-row>Add row</button>
          <button class="podcast-save-button" type="submit">Save Podcasts</button>
        </div>
      </form>
    </section>
    """.format(rows="\n".join(rows), csrf_input=render_csrf_input(csrf_token))


def render_podcast_row(index: int, podcast: PodcastConfig | None) -> str:
    podcast = podcast or PodcastConfig(url="")
    metadata = podcast_metadata_label(podcast)
    return """
    <div class="podcast-row">
      <label class="field" for="podcast_url_{index}">
        Podcast URL
        <input id="podcast_url_{index}" name="podcast_url" type="url" value="{url}" placeholder="https://podcasts.apple.com/…" autocapitalize="none" spellcheck="false">
      </label>
      <button class="secondary" type="button" data-remove-podcast-row aria-label="Remove podcast URL">Remove</button>
      <p class="podcast-meta muted">{metadata}</p>
    </div>
    """.format(
        index=index,
        url=html.escape(podcast.url),
        metadata=html.escape(metadata),
    )


def podcast_metadata_label(podcast: PodcastConfig) -> str:
    if podcast.title and podcast.author:
        return f"{podcast.title} by {podcast.author}"
    if podcast.title:
        return podcast.title
    return ""


def render_screen_saver_options(selected_mode: str) -> str:
    options = (
        (SCREEN_SAVER_CHAPTER, "Chapter word"),
        (SCREEN_SAVER_BOOKS, "Book titles"),
        (SCREEN_SAVER_CLOCK, "Digital clock"),
    )
    return "\n".join(
        '<option value="{value}"{selected}>{label}</option>'.format(
            value=html.escape(value),
            selected=" selected" if value == selected_mode else "",
            label=html.escape(label),
        )
        for value, label in options
    )


def render_library_sort_options(selected_mode: str) -> str:
    options = (
        (LIBRARY_SORT_TITLE, "Title"),
        (LIBRARY_SORT_AUTHOR, "Author (last name)"),
        (LIBRARY_SORT_AUTHOR_FIRST, "Author (first name)"),
    )
    return "\n".join(
        '<option value="{value}"{selected}>{label}</option>'.format(
            value=value,
            selected=" selected" if value == selected_mode else "",
            label=label,
        )
        for value, label in options
    )


def render_playback_speed_options(selected_speed: float) -> str:
    return "\n".join(
        '<option value="{value}"{selected}>{label}</option>'.format(
            value=f"{speed:g}",
            selected=" selected" if speed == selected_speed else "",
            label=f"{speed:g}x",
        )
        for speed in PLAYBACK_SPEEDS
    )


def render_screen_saver_dim_options(selected_percent: int) -> str:
    labels = {
        5: "5% (darkest)",
        50: "50% (brightest)",
    }
    return "\n".join(
        '<option value="{value}"{selected}>{label}</option>'.format(
            value=value,
            selected=" selected" if value == selected_percent else "",
            label=labels.get(value, f"{value}%"),
        )
        for value in SCREEN_SAVER_DIM_LEVELS
    )


def render_screen_saver_clock_options(selected_format: str) -> str:
    now = datetime.now()
    return "\n".join(
        '<option value="{value}"{selected}>{value} ({preview})</option>'.format(
            value=clock_format,
            selected=" selected" if clock_format == selected_format else "",
            preview=html.escape(clock_screen_saver_text(now, clock_format=clock_format)),
        )
        for clock_format in (CLOCK_FORMAT_12_HOUR, CLOCK_FORMAT_24_HOUR)
    )


def render_player_status_value(status: WebPlayerStatus, csrf_token: str = "") -> str:
    volume = min(max(status.volume_percent, 0), 100)
    state = player_state_label(status)
    button_action = "pause" if status.is_playing else "play"
    button_label = "Pause on Chapter" if status.is_playing else "Play on Chapter"
    title = html.escape(status.title) if status.has_session else "Nothing playing"
    meta = (
        f"{html.escape(state)} · {html.escape(render_time_info(status))}"
        if status.has_session
        else ""
    )
    hidden_class = "" if status.has_session else " hidden"
    return """
    <div class="status-value" data-now-playing data-has-session="{has_session}">
      <div class="now-playing-summary">
        <div class="cover-with-controls">
          {cover}
          <form class="cover-play" data-playback-form method="post" action="/player">
            {csrf_input}
            <input data-playback-action type="hidden" name="action" value="{button_action}">
            <button class="cover-button" data-playback-button type="submit" aria-label="{button_label}" title="{button_label}">
              {icon}
            </button>
          </form>
          <div class="cover-feedback"><p class="hint" data-playback-pending role="status"></p></div>
        </div>
        <div>
          <h3 data-now-playing-title>{title}</h3>
          <p class="now-playing-meta muted" data-now-playing-meta>{meta}</p>
          <p class="muted" data-overview-value="listeningSource"{source_hidden}>{source}</p>
          <progress data-listening-progress aria-label="Listening progress" max="1" value="{progress}"{progress_hidden}></progress>
        </div>
      </div>
      <div data-command-feedback="playback" role="status"></div>
      <div data-command-feedback="volume" role="status"></div>
      <form class="volume-form{hidden_class}" data-volume-form method="post" action="/player">
        {csrf_input}
        <input type="hidden" name="action" value="volume">
        <label class="volume-label" for="overview-volume">Volume</label>
        <input id="overview-volume" class="volume-slider" data-volume-slider name="volume" type="range" min="0" max="100" value="{volume}">
        <div class="volume-percent" data-volume-percent>{volume}%</div>
      </form>
    </div>
    """.format(
        has_session="1" if status.has_session else "0",
        cover=render_cover(status.cover_item_id, lazy=False),
        title=title,
        meta=meta,
        source=listening_source(status),
        source_hidden=hidden_class,
        progress=min(1, max(0, status.current_time / status.duration)) if status.duration > 0 else 0,
        progress_hidden="" if status.has_session and status.duration > 0 else " hidden",
        hidden_class=hidden_class,
        volume=volume,
        button_action=button_action,
        button_label=button_label,
        icon=cover_icon(button_action),
        csrf_input=render_csrf_input(csrf_token),
    )


def render_time_info(status: WebPlayerStatus) -> str:
    if status.duration <= 0:
        return format_duration(status.current_time)
    return f"{format_duration(status.current_time)} / {format_duration(status.duration)}"


def player_state_label(status: WebPlayerStatus) -> str:
    if status.muted or status.volume_percent <= 0:
        return "Muted"
    return "Playing" if status.is_playing else "Paused"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def wifi_status_class(wifi: WifiStatus | None) -> str:
    if wifi is None:
        return ""
    return "ok" if wifi.connected else "bad"


def render_wifi_status_value(wifi: WifiStatus | None) -> str:
    if wifi is None:
        return "Checking Wi-Fi…"
    if wifi.connected and wifi.ssid:
        return f"Connected to <strong>{html.escape(wifi.ssid)}</strong>"
    return html.escape(wifi.message or "Unknown")


def render_connection_health(status: ConnectionHealth) -> str:
    state = status.state
    label, detail = {
        ConnectionState.NOT_CONFIGURED: ("Not configured", "Sign in from Connections."),
        ConnectionState.CHECKING: ("Checking connection", "Verifying sign-in and library access."),
        ConnectionState.CHECK_UNAVAILABLE: ("Check unavailable", "Chapter will retry the connection check."),
        ConnectionState.UNREACHABLE: ("Server unreachable", "Check Wi-Fi, the server and its address."),
        ConnectionState.TIMED_OUT: ("Connection timed out", "Check the server. Chapter retries automatically."),
        ConnectionState.SIGN_IN_REQUIRED: ("Sign-in required", "Sign in again from Connections."),
        ConnectionState.LIBRARY_UNAVAILABLE: (
            "Library unavailable",
            "Check that this account can access the library in Audiobookshelf.",
        ),
        ConnectionState.SERVER_ERROR: ("Unexpected server response", "Check the Audiobookshelf server address and status."),
        ConnectionState.CONNECTED: ("Library accessible", ""),
    }[state]
    tone = "ok" if state == ConnectionState.CONNECTED else "bad" if status.checked_at else ""
    parts = [f'<strong class="{tone}">{label}</strong>']
    if status.username:
        parts.append(f"Signed in as {html.escape(status.username)}.")
    if detail:
        parts.append(detail)
    if status.checked_at:
        checked = status.checked_at
        parts.append(f'Last checked <time datetime="{checked.isoformat()}">{checked:%d %b, %H:%M:%S %Z}</time>.')
    return "<br>".join(parts)


def render_bluetooth_status_value(message: str) -> str:
    prefix = "Connected to "
    if message.startswith(prefix):
        device_name = message[len(prefix) :]
        return f"Connected to <strong>{html.escape(device_name)}</strong>"
    return html.escape(message)


def wifi_card_key(wifi: WifiStatus | None) -> str:
    if wifi is None:
        return "checking"
    if wifi.connected and wifi.ssid and wifi.ssid != SETUP_HOTSPOT_SSID:
        return "connected/" + wifi.ssid
    return "join"


def render_wifi_card(wifi: WifiStatus | None, csrf_token: str = "") -> str:
    key = wifi_card_key(wifi)
    if wifi is None:
        return ('<section class="divider-section" aria-labelledby="wifi-heading" data-wifi-card="checking">'
                '<h2 id="wifi-heading">Wi-Fi</h2><p role="status">Checking Wi-Fi…</p>'
                '<noscript><a href="/?tab=connections">Check again</a></noscript></section>')
    if key != "join":
        return """
        <section class="divider-section" aria-labelledby="wifi-heading" data-wifi-card="{card_key}">
          <h2 id="wifi-heading">Wi-Fi</h2>
          <p class="hint">Connected to <strong>{ssid}</strong>.</p>
          <p class="hint">Forgetting this network will make the device form its own open Wi-Fi network. Connect to <code>{hotspot_ssid}</code>, then browse to <code>{hotspot_url}</code>.</p>
          <form method="post" action="/wifi/forget">
            {csrf_input}
            <button class="secondary" type="submit">Forget this WiFi network</button>
          </form>
        </section>
        """.format(
            ssid=html.escape(wifi.ssid), card_key=html.escape(key, quote=True),
            hotspot_ssid=html.escape(SETUP_HOTSPOT_SSID),
            hotspot_url=html.escape(SETUP_HOTSPOT_URL),
            csrf_input=render_csrf_input(csrf_token),
        )

    return """
    <section class="divider-section" aria-labelledby="wifi-heading" data-wifi-card="join">
      <h2 id="wifi-heading">Wi-Fi</h2>
      <p class="hint">Connect to the open Wi-Fi network <code>{hotspot_ssid}</code>, then browse to <code>{hotspot_url}</code>.</p>
      <form class="form-stack connection-form" method="post" action="/wifi">
        {csrf_input}
        <label class="field" for="wifi_ssid">Wi-Fi network name
          <input id="wifi_ssid" name="wifi_ssid" autocomplete="off" required>
        </label>
        <label class="field" for="wifi_password">Wi-Fi password
          <input id="wifi_password" name="wifi_password" type="password" autocomplete="current-password">
        </label>

        <button type="submit">Connect to Wi-Fi</button>
      </form>
    </section>
    """.format(
        hotspot_ssid=html.escape(SETUP_HOTSPOT_SSID),
        hotspot_url=html.escape(SETUP_HOTSPOT_URL),
        csrf_input=render_csrf_input(csrf_token),
    )
