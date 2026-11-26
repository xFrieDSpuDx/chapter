"""Shared shell and navigation for Chapter's browser admin pages."""

from html import escape
from pathlib import Path

from .player_control import load_web_player_status
from .web_artwork import artwork_url


PACKAGE_DIR = Path(__file__).resolve().parent
ASSET_DIR = PACKAGE_DIR / "assets"
TEMPLATE_DIR = PACKAGE_DIR / "templates"
PAGE = (TEMPLATE_DIR / "admin.html").read_text(encoding="utf-8")
ADMIN_STYLES = "\n".join(
    (ASSET_DIR / name).read_text(encoding="utf-8") for name in ("admin_palettes.css", "admin.css")
)
ARTWORK_SCRIPT = (ASSET_DIR / "artwork.js").read_text(encoding="utf-8")
THEME_SCRIPT = (ASSET_DIR / "admin_theme.js").read_text(encoding="utf-8")
MENU_SCRIPT = (ASSET_DIR / "admin_menu.js").read_text(encoding="utf-8")
PLAYER_NAVIGATION_SCRIPT = (ASSET_DIR / "player_navigation.js").read_text(encoding="utf-8")

DEFAULT_PALETTE = "taupe-gold"
ADMIN_PALETTES = {
    DEFAULT_PALETTE: "Taupe & Gold",
    "fjord": "Fjord",
    "juniper": "Juniper",
    "plum-linen": "Plum and Linen",
    "terracotta": "Terracotta",
    "monochrome": "Monochrome",
    "tangerine-charcoal": "Tangerine & Charcoal",
    "midnight-brass": "Midnight Brass",
}

ADMIN_SECTIONS = (
    ("overview", "Overview", "/?tab=overview"),
    ("player", "Player", "/player"),
    ("library", "Library", "/library"),
    ("downloads", "Downloads", "/library?downloaded=1&amp;section=downloads"),
    ("settings", "Settings", "/?tab=settings"),
    ("connections", "Connections", "/?tab=connections"),
    ("podcasts", "Podcasts", "/?tab=podcasts"),
    ("system", "System", "/?tab=system"),
)
DASHBOARD_TABS = frozenset(section for section, _, url in ADMIN_SECTIONS if url.startswith("/?tab="))


def render_admin_page(
    title: str,
    section: str,
    content: str,
    *,
    styles: str = "",
    scripts: str = "",
    csrf_token: str = "",
    guest_sections: tuple[str, ...] | None = None,
    parent: tuple[str, str] | None = None,
) -> str:
    navigation = []
    home_url = "/?tab=overview"
    player = load_web_player_status() if guest_sections is None else None
    has_session = player and (player.has_session or player.opening_key)
    for key, label, url in ADMIN_SECTIONS:
        if guest_sections is not None and key not in guest_sections:
            continue
        current = ' aria-current="page"' if key == section else ""
        tab_target = f' data-tab-target="{key}"' if key in DASHBOARD_TABS else ""
        player_nav = (' data-player-navigation' + ("" if has_session else " hidden")) if key == "player" else ""
        navigation.append(f'<a href="{url}"{current}{tab_target}{player_nav}>{label}</a>')
    if guest_sections is not None:
        current = ' aria-current="page"' if section == "owner" else ""
        navigation.append(f'<a href="/owner" class="login-link"{current}>Log in</a>')
        if "overview" not in guest_sections:
            home_url = "/library" if "library" in guest_sections else "/owner"
    else:
        scripts += f"<script>{PLAYER_NAVIGATION_SCRIPT}</script>"
    if parent:
        label, url = parent
        content = f'<a class="text-action back-link" href="{escape(url)}">{escape(label)}</a>\n{content}'
    return PAGE.format(
        title=escape(title),
        home_url=home_url,
        navigation="\n".join(navigation),
        content=content,
        styles=ADMIN_STYLES + styles,
        scripts=f"<script>{MENU_SCRIPT}</script><script>{ARTWORK_SCRIPT}</script>" + scripts,
        csrf_token=escape(csrf_token, quote=True),
        theme_script=THEME_SCRIPT,
        default_palette=DEFAULT_PALETTE,
        palette_ids=" ".join(ADMIN_PALETTES),
        palette_choices="".join(
            f'<option value="{key}">{escape(name)}</option>' for key, name in ADMIN_PALETTES.items()
        ),
    )


def render_cover(item_id: str, *, lazy: bool = True) -> str:
    url = artwork_url(item_id)
    source = f' src="{url}"' if url else ' hidden'
    loading = ' loading="lazy"' if lazy else ''
    return (
        '<span class="cover-frame"><span class="book-symbol" aria-hidden="true"></span>'
        f'<img data-book-cover alt="" decoding="async"{loading}{source}></span>'
    )


def cover_icon(name: str) -> str:
    return f'<span class="cover-icon icon-{name}" aria-hidden="true"></span>'
