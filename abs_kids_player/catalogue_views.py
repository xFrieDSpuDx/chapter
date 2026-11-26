"""Render the library picker with the current on-device refresh status."""

from __future__ import annotations

from html import escape

from .admin_layout import ASSET_DIR, TEMPLATE_DIR, render_admin_page
from .catalogue_settings import CatalogueStatus, catalogue_request_key, format_last_refresh
from .config import AppConfig
from .setup_views import render_csrf_input


CATALOGUE_PAGE = (TEMPLATE_DIR / "catalogue_settings.html").read_text(encoding="utf-8")
CATALOGUE_SCRIPT = (ASSET_DIR / "catalogue_settings.js").read_text(encoding="utf-8")


def render_catalogue_settings(
    config: AppConfig, status: CatalogueStatus, csrf_token: str, request_id: str = "",
) -> str:
    key = catalogue_request_key(config) if config.is_ready else ""
    state = "Waiting for Chapter to refresh the library…" if request_id else (
        f"Last refresh failed. {status.error}." if status.error else ""
    )
    content = CATALOGUE_PAGE.format(
        library_id=escape(config.library_id, quote=True),
        library_name=escape(config.library_name or ("Audiobookshelf library" if config.is_ready else "Not configured")),
        request_id=escape(request_id, quote=True),
        last_refresh=escape(format_last_refresh(status)),
        status_text=escape(state),
        options_text="Loading available audiobook libraries…" if config.is_ready else "Sign in to Audiobookshelf first.",
        csrf=render_csrf_input(csrf_token),
        catalogue_key=f'<input name="catalogue_key" type="hidden" value="{key}">',
        disabled="" if config.is_ready else " disabled",
    )
    return render_admin_page(
        "Audiobookshelf library · Chapter", "settings", content,
        scripts=f"<script>{CATALOGUE_SCRIPT}</script>", parent=("Books", "/settings/books"),
    )
