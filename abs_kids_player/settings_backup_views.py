"""Render the settings import form and its confirmation."""

from html import escape

from .admin_layout import TEMPLATE_DIR, render_admin_page
from .config import AppConfig
from .settings_backup import LABELS
from .setup_views import render_csrf_input


PAGE = (TEMPLATE_DIR / "settings_restore.html").read_text(encoding="utf-8")
DISPLAY_VALUES = {
    "author": "Author last name", "author_first": "Author first name", "title": "Title",
    "chapter": "Chapter", "books": "Books", "clock": "Clock", "schedule": "Automatic",
    "on": "On", "off": "Off",
}


def display_setting(value: object) -> str:
    if isinstance(value, bool):
        return "On" if value else "Off"
    return escape(DISPLAY_VALUES.get(value, str(value)) if value != "" else "None")


def render_settings_restore(
    csrf: str, *, values: dict | None = None, current: AppConfig | None = None, raw: str = "", ticket: str = "",
    shelf_label: str = "", notice: str = "", restored: bool = False,
) -> str:
    token = render_csrf_input(csrf)
    message = f'<p role="status">{escape(notice)}</p>' if notice else ""
    if restored:
        content = '<div><a class="text-action" href="/?tab=system">Return to System</a></div>'
    elif values is None or current is None:
        content = f"""
<form class="form-stack" method="post" action="/system/settings-preview">
{token}
<label class="field file-field">Choose a backup file
  <input id="backup-file" type="file" accept=".json,application/json">
</label>
<label class="field" for="backup">Settings backup JSON
  <textarea id="backup" name="backup" rows="6" required maxlength="8192" spellcheck="false"
    autocapitalize="none" placeholder="Paste the backup here or choose a file above"></textarea>
</label>
<div class="form-footer"><button type="submit">Preview settings</button></div>
</form>
<script>
document.getElementById("backup-file").addEventListener("change", async (event) => {{
  const file = event.target.files[0];
  const input = document.getElementById("backup");
  if (!file) return;
  input.value = "";
  if (file.size > 8192) {{
    input.setCustomValidity("The settings backup is too large.");
    input.reportValidity();
    return;
  }}
  input.setCustomValidity("");
  try {{ input.value = await file.text(); }}
  catch {{ input.setCustomValidity("The backup could not be read."); input.reportValidity(); }}
}});
document.getElementById("backup").addEventListener("input", (event) => event.target.setCustomValidity(""));
</script>"""
    else:
        rows = "".join(
            f'<li><h2>{LABELS[name]}</h2><dl class="change-values">'
            f'<div><dt>Current</dt><dd>{display_setting(getattr(current, name))}</dd></div>'
            f'<div><dt>Restore to</dt><dd>{escape(shelf_label) if name == "pinned_shelf" and shelf_label else display_setting(value)}</dd></div>'
            '</dl></li>'
            for name, value in values.items() if getattr(current, name) != value
        )
        if not rows:
            content = '<p>No settings need to change.</p><div><a class="text-action" href="/system/settings-restore">Choose another backup</a></div>'
        else:
            content = f"""
<ul class="record-list row-dividers restore-changes">{rows}</ul>
<form class="form-stack" method="post" action="/system/settings-restore">
{token}
<input type="hidden" name="backup" value="{escape(raw, quote=True)}">
<input type="hidden" name="preview" value="{escape(ticket, quote=True)}">
<div class="form-footer">
  <button type="submit">Restore these settings</button>
  <a class="button secondary" href="/system/settings-restore">Cancel</a>
</div>
</form>"""
    page = PAGE.format(content=content, message=message)
    return render_admin_page("Restore settings · Chapter", "system", page, parent=("System", "/?tab=system"))
