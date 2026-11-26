"""Render Chapter's local owner sign-in and recovery instructions."""

from html import escape

from .admin_layout import render_admin_page
from .owner_auth import DEFAULT_OWNER_USERNAME, OwnerCredential
from .setup_views import render_csrf_input


def render_owner_page(
    csrf_token: str, *, configured: bool, signed_in: bool, error: str = "", updated: bool = False,
    guest_sections: tuple[str, ...] | None = None,
) -> str:
    fields = render_csrf_input(csrf_token)
    feedback = f'<p class="hint" role="alert">{escape(error)}</p>' if error else ""
    recovery = (
        '<p class="hint">On the Pi, run this command from the Chapter installation directory '
        'to set or reset the owner password.</p>'
        '<pre><code>sudo python3 -m abs_kids_player.owner_auth set-password</code></pre>'
    )
    if not configured:
        content = '<p>Owner login is off.</p>' + recovery
    elif signed_in:
        content = (
            '<p>You are logged in as the owner.</p>'
            f'<form method="post" action="/owner/sign-out">{fields}'
            '<button type="submit">Sign out</button></form>' + recovery
        )
    else:
        message = '<p role="status">Owner details saved. Log in with your new details.</p>' if updated else ""
        content = (
            f'{message}<form class="form-stack short-form" method="post" action="/owner/sign-in">{fields}'
            '<label class="field">Username'
            '<input name="username" maxlength="64" autocomplete="username" autocapitalize="none" required>'
            '</label>'
            '<label class="field">Owner password'
            '<input name="password" type="password" maxlength="128" autocomplete="current-password" required>'
            '</label><button type="submit">Log in</button></form>' + recovery
        )
    heading = "Log in" if configured and not signed_in else "Owner access"
    parent = None
    if guest_sections is None:
        parent = ("Security", "/settings/security")
    elif "overview" in guest_sections:
        parent = ("Overview", "/?tab=overview")
    elif "library" in guest_sections:
        parent = ("Library", "/library")
    return render_admin_page(
        f"{heading} · Chapter", "owner",
        f'<header class="page-heading"><h1>{heading}</h1></header>'
        f'<section class="divider-section owner-page">{feedback}{content}</section>',
        guest_sections=guest_sections, parent=parent,
    )


def render_security_page(
    csrf_token: str, credential: OwnerCredential | None, *, saved: bool = False, error: str = "",
) -> str:
    fields = render_csrf_input(csrf_token)
    feedback = f'<p class="message error" role="alert">{escape(error)}</p>' if error else ""
    if saved:
        feedback = '<p class="message" role="status">Visitor access saved.</p>'
    account = render_owner_account(fields, credential)
    visitors = render_visitor_access(fields, credential)
    return render_admin_page(
        "Security · Chapter", "settings",
        f'<header class="page-heading"><h1>Security</h1></header>{feedback}'
        f'<div class="divider-stack">{account}{visitors}</div>',
        parent=("All settings", "/?tab=settings"),
    )


def render_owner_account(fields: str, credential: OwnerCredential | None) -> str:
    username = escape(credential.username if credential else DEFAULT_OWNER_USERNAME)
    current_password = (
        '<label class="field">Current password'
        '<input type="password" name="current_password" maxlength="128" autocomplete="current-password" required></label>'
        if credential else ""
    )
    button = "Save new login details" if credential else "Require owner login"
    description = "Owner login is required for administration." if credential else "Owner login is currently off. Set a username and password to secure administration."
    sign_out = (
        f'<form method="post" action="/owner/sign-out">{fields}<button class="secondary" type="submit">Sign out</button></form>'
        if credential else ""
    )
    return f"""
    <section class="divider-section" aria-labelledby="owner-account">
      <h2 id="owner-account">Owner account</h2>
      <p class="muted">{description}</p>
      <form class="form-stack short-form" method="post" action="/settings/security/account">
        {fields}
        <label class="field">Username
          <input name="username" value="{username}" maxlength="64" autocomplete="username" autocapitalize="none" required>
        </label>
        {current_password}
        <label class="field">New password
          <input name="password" type="password" minlength="10" maxlength="128" autocomplete="new-password" required>
        </label>
        <label class="field">Confirm new password
          <input name="confirm_password" type="password" minlength="10" maxlength="128" autocomplete="new-password" required>
        </label>
        <button type="submit">{button}</button>
      </form>
      {sign_out}
    </section>
    """


def render_visitor_access(fields: str, credential: OwnerCredential | None) -> str:
    if credential is None:
        controls = '<p class="muted">Set up the owner account to choose which pages visitors can read.</p>'
    else:
        overview = " checked" if credential.public_overview else ""
        library = " checked" if credential.public_library else ""
        controls = f"""
        <form class="form-stack visitor-access" method="post" action="/settings/security/access">
          {fields}
          <label class="check-field"><input name="public_overview" type="checkbox" value="1"{overview}>
            <span>Overview <small class="muted">Current book, Night mode and download counts</small></span>
          </label>
          <label class="check-field"><input name="public_library" type="checkbox" value="1"{library}>
            <span>Library and Downloads <small class="muted">Book details, artwork, listening progress and download status</small></span>
          </label>
          <p class="muted">Visitor access is read-only. Settings, Connections, podcast setup, System, playback controls and download actions require owner login.</p>
          <button type="submit">Save visitor access</button>
        </form>
        """
    return f'<section class="divider-section" aria-labelledby="visitor-access"><h2 id="visitor-access">Access without login</h2>{controls}</section>'
