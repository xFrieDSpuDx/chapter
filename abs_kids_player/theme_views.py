"""Render the browser's colour palette choices."""

from html import escape

from .admin_layout import (
    ADMIN_PALETTES,
    ASSET_DIR,
    DEFAULT_PALETTE,
    TEMPLATE_DIR,
    render_admin_page,
)


PAGE = (TEMPLATE_DIR / "themes.html").read_text(encoding="utf-8")
STYLES = (ASSET_DIR / "themes.css").read_text(encoding="utf-8")


def render_palette_option(palette: str, name: str) -> str:
    default = palette == DEFAULT_PALETTE
    checked = " checked" if default else ""
    default_label = " <small>Default</small>" if default else ""
    return (
        '<label class="palette-row">'
        '<span class="palette-name">'
        f'<input type="radio" name="palette" value="{palette}"{checked}>'
        f'<span>{escape(name)}{default_label}</span>'
        '</span>'
        f'<span class="palette-sample" data-palette="{palette}" data-mode="light" aria-hidden="true"></span>'
        f'<span class="palette-sample" data-palette="{palette}" data-mode="dark" aria-hidden="true"></span>'
        '</label>'
    )


def render_themes_page() -> str:
    options = "\n".join(render_palette_option(key, name) for key, name in ADMIN_PALETTES.items())
    return render_admin_page(
        "Themes · Chapter", "settings", PAGE.format(options=options),
        styles=STYLES, parent=("All settings", "/?tab=settings"),
    )
