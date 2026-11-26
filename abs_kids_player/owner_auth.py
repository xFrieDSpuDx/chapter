"""Local owner credentials and browser sessions for the setup server."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from getpass import getpass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

from .storage import ensure_storage_directory, write_json_atomically


COOKIE_NAME = "chapter_owner"
SESSION_SECONDS = 12 * 60 * 60
MAX_SESSIONS = 32
PASSWORD_BYTES = 128
DEFAULT_OWNER_USERNAME = "admin"


@dataclass(frozen=True)
class OwnerCredential:
    salt: str
    digest: str
    username: str = DEFAULT_OWNER_USERNAME
    public_overview: bool = False
    public_library: bool = False

    @property
    def account_key(self) -> tuple[str, str, str]:
        return self.username, self.salt, self.digest


@dataclass(frozen=True)
class OwnerAccess:
    credential: OwnerCredential | None = None
    signed_in: bool = False
    unreadable: bool = False

    @property
    def can_manage(self) -> bool:
        return not self.unreadable and (self.credential is None or self.signed_in)

    @property
    def public_sections(self) -> tuple[str, ...]:
        if self.credential is None:
            return ()
        overview = ("overview",) if self.credential.public_overview else ()
        library = ("library", "downloads") if self.credential.public_library else ()
        return overview + library


def credential_path() -> Path:
    return Path(os.environ.get("CHAPTER_OWNER_FILE", "/var/lib/chapter-owner/credential.json"))


def _hash(password: bytes, salt: bytes) -> str:
    return hashlib.scrypt(password, salt=salt, n=16384, r=8, p=1, dklen=32).hex()


def load_credential() -> OwnerCredential | None:
    try:
        data = json.loads(credential_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    try:
        credential = OwnerCredential(
            salt=data["salt"], digest=data["digest"],
            username=validated_owner_username(data.get("username", DEFAULT_OWNER_USERNAME)),
            public_overview=data.get("public_overview") is True,
            public_library=data.get("public_library") is True,
        )
        if len(bytes.fromhex(credential.salt)) == 16 and len(bytes.fromhex(credential.digest)) == 32:
            return credential
    except (KeyError, TypeError, ValueError):
        pass
    raise ValueError("Invalid Chapter owner credential")


def validated_owner_username(username: str) -> str:
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username.strip()):
        raise ValueError("Use 1 to 64 letters, numbers, dots, underscores or hyphens for the username.")
    return username.strip()


def credential_with_password(current: OwnerCredential | None, username: str, password: str) -> OwnerCredential:
    username = validated_owner_username(username)
    encoded = password.encode("utf-8")
    if len(password) < 10 or len(encoded) > PASSWORD_BYTES:
        raise ValueError("Use at least 10 characters and at most 128 UTF-8 bytes.")
    salt = secrets.token_bytes(16)
    return OwnerCredential(
        salt.hex(), _hash(encoded, salt), username,
        current.public_overview if current else False, current.public_library if current else False,
    )


def password_matches(credential: OwnerCredential, password: str) -> bool:
    encoded = password.encode("utf-8")
    return bool(encoded and len(encoded) <= PASSWORD_BYTES and hmac.compare_digest(
        _hash(encoded, bytes.fromhex(credential.salt)), credential.digest,
    ))


@contextmanager
def credential_write_lock():
    path = credential_path()
    ensure_storage_directory(path.parent, shared_owner=False)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save_credential(credential: OwnerCredential) -> None:
    write_json_atomically(credential_path(), asdict(credential), durable=True, shared_owner=False)


def set_password(password: str, username: str | None = None) -> None:
    with credential_write_lock():
        try:
            current = load_credential()
        except ValueError:
            current = None
        username = username if username is not None else (current.username if current else DEFAULT_OWNER_USERNAME)
        save_credential(credential_with_password(current, username, password))


def change_owner_login(
    username: str, password: str, *, expected: OwnerCredential | None, current_password: str,
) -> None:
    with credential_write_lock():
        require_current_credential(expected)
        if expected is not None and not password_matches(expected, current_password):
            raise ValueError("The current password is incorrect.")
        save_credential(credential_with_password(expected, username, password))


def change_public_access(expected: OwnerCredential, *, overview: bool, library: bool) -> None:
    with credential_write_lock():
        require_current_credential(expected)
        save_credential(replace(expected, public_overview=overview, public_library=library))


def require_current_credential(expected: OwnerCredential | None) -> None:
    if load_credential() != expected:
        raise ValueError("Owner settings changed. Reload Security and try again.")


def _session_token(cookie_header: str) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except CookieError:
        return ""
    return cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ""


def session_cookie(token: str) -> str:
    age = SESSION_SECONDS if token else 0
    return f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}"


class OwnerSessions:
    def __init__(self) -> None:
        self._sessions: dict[str, tuple[float, tuple[str, str, str]]] = {}
        self._lock = threading.Lock()
        self._next_attempt = 0.0

    def allows(self, cookie_header: str) -> bool:
        return self.access(cookie_header).can_manage

    def access(self, cookie_header: str) -> OwnerAccess:
        try:
            credential = load_credential()
        except (OSError, ValueError):
            return OwnerAccess(unreadable=True)
        if credential is None:
            return OwnerAccess()
        with self._lock:
            session = self._sessions.get(_session_token(cookie_header))
            signed_in = bool(session and session[0] > time.monotonic() and session[1] == credential.account_key)
        return OwnerAccess(credential, signed_in)

    def sign_in(self, username: str, password: str) -> str:
        with self._lock:
            now = time.monotonic()
            if now < self._next_attempt:
                return ""
            self._next_attempt = now + 1
            try:
                credential = load_credential()
                if credential is None or not password_matches(credential, password) or username.strip() != credential.username:
                    return ""
            except (OSError, ValueError):
                return ""
            token = secrets.token_urlsafe(32)
            self._sessions = {
                key: session for key, session in self._sessions.items()
                if session[0] > now and session[1] == credential.account_key
            }
            if len(self._sessions) >= MAX_SESSIONS:
                del self._sessions[next(iter(self._sessions))]
            self._sessions[token] = (now + SESSION_SECONDS, credential.account_key)
            self._next_attempt = 0
            return token

    def sign_out(self, cookie_header: str) -> None:
        with self._lock:
            self._sessions.pop(_session_token(cookie_header), None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Set or recover Chapter's local owner password.")
    parser.add_argument("command", choices=("set-password",))
    parser.add_argument("--username", help="Set a new owner username. The first account defaults to admin.")
    args = parser.parse_args()
    if os.geteuid() != 0 and "CHAPTER_OWNER_FILE" not in os.environ:
        parser.error("Run this command with sudo on the Chapter device.")
    password = getpass("New Chapter owner password: ")
    if password != getpass("Confirm password: "):
        parser.error("Passwords differ.")
    try:
        set_password(password, args.username)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print("Owner password saved. Previous browser sessions have been signed out.")


if __name__ == "__main__":
    main()
