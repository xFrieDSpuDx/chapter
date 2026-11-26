"""Render the dedicated browser player and its listening queue."""

from html import escape

from .admin_layout import ASSET_DIR, TEMPLATE_DIR, cover_icon, render_admin_page, render_cover
from .config import PLAYBACK_SPEEDS, AppConfig
from .library import next_series_book
from .listening_queue import ListeningQueue
from .models import Book
from .player_control import WebPlayerStatus
from .setup_views import render_csrf_input


PAGE = (TEMPLATE_DIR / "player.html").read_text(encoding="utf-8")
STYLES = (ASSET_DIR / "player.css").read_text(encoding="utf-8")
SCRIPT = (ASSET_DIR / "player.js").read_text(encoding="utf-8")
COMMAND_SCRIPT = (ASSET_DIR / "player_commands.js").read_text(encoding="utf-8")
BOOKMARK_SCRIPT = (ASSET_DIR / "player_bookmarks.js").read_text(encoding="utf-8")


def render_player_page(config: AppConfig, status: WebPlayerStatus, queue: str, csrf: str) -> str:
    speed = status.playback_rate if status.session_key else config.playback_speed
    content = PAGE.format(
        title=escape(status.title),
        author=escape(status.author),
        cover=render_cover(status.cover_item_id, lazy=False),
        icon=cover_icon("pause" if status.is_playing else "play"),
        toggle="Pause" if status.is_playing else "Play",
        playing="Now playing" if status.is_playing else "Paused",
        volume=0 if status.muted else status.volume_percent,
        mute="Unmute" if status.muted else "Mute",
        opening_key=escape(status.opening_key),
        opening_hidden="" if status.opening_key else " hidden",
        session_hidden="" if status.has_session and not status.opening_key else " hidden",
        idle_hidden=" hidden" if status.has_session or status.opening_key else "",
        queue_hidden=" hidden" if status.has_session or status.opening_key else "",
        speeds="".join(
            f'<option value="{round(rate * 100)}"{" selected" if rate == speed else ""}>{rate:g}×</option>'
            for rate in PLAYBACK_SPEEDS
        ),
        skip_back=config.skip_back_seconds,
        skip_forward=config.skip_forward_seconds,
        queue=queue,
    )
    return render_admin_page("Player · Chapter", "player", content, styles=STYLES,
                             scripts=f"<script>{BOOKMARK_SCRIPT}\n{COMMAND_SCRIPT}\n{SCRIPT}</script>", csrf_token=csrf)


def queue_book_form(book_id: str, csrf: str, action: str, label: str, *, disabled: bool = False, series: str = "") -> str:
    series_field = f'<input type="hidden" name="series" value="{escape(series)}">' if series else ""
    return (
        '<form method="post" action="/player/queue" data-listening-queue-form>'
        f'{render_csrf_input(csrf)}<input type="hidden" name="book_id" value="{escape(book_id)}">'
        f'<input type="hidden" name="action" value="{action}">{series_field}'
        f'<button class="secondary" type="submit"{" disabled" if disabled else ""}>{escape(label)}</button></form>'
    )


def render_player_queue(
    order: ListeningQueue, books: list[Book], active_item: str, csrf: str, series: str = "",
) -> str:
    available = {book.id: book for book in books if not book.episode_id}
    items = []
    for index, item in enumerate(order.items):
        book = available.get(item)
        name = book.title if book else "Unavailable book"
        forms = (
            queue_book_form(item, csrf, "play", "Play now", disabled=book is None)
            + queue_book_form(item, csrf, "up", "Move up", disabled=index == 0)
            + queue_book_form(item, csrf, "down", "Move down", disabled=index + 1 == len(order.items))
            + queue_book_form(item, csrf, "remove", "Remove")
        )
        items.append(f'<li><strong>{escape(name)}</strong><div class="actions">{forms}</div></li>')
    entries = (
        f'<ol class="listening-queue-list row-dividers">{"".join(items)}</ol>'
        if items else '<p class="muted">The listening queue is empty.</p>'
    )
    suggested = next_series_book(list(available.values()), active_item, series) if active_item else None
    next_book = ""
    if suggested:
        already_queued = suggested.id in order.items
        controls = queue_book_form(
            suggested.id, csrf, "add", "Queued" if already_queued else "Add to queue", disabled=already_queued,
        ) + queue_book_form(suggested.id, csrf, "play", "Play now", series=series)
        next_book = f'<div class="series-next"><p class="muted">Next in series</p><strong>{escape(suggested.title)}</strong><div class="actions">{controls}</div></div>'
    auto = " checked" if order.auto_advance else ""
    return (
        f'{entries}{next_book}<form method="post" action="/player/queue" data-listening-queue-form>'
        f'{render_csrf_input(csrf)}<input type="hidden" name="action" value="auto">'
        f'<label class="check-field"><input type="checkbox" name="enabled" value="1"{auto}>'
        '<span>Automatically play the next queued or series book</span></label>'
        '<button class="secondary" type="submit">Save auto-advance</button></form>'
    )
