"""Verify Audiobookshelf access without waiting on it in an HTTP handler."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Callable
from urllib.error import HTTPError, URLError

from .api import AudiobookshelfError, client_from_config
from .config import AppConfig, remember_verified_username


class ConnectionState(str, Enum):
    NOT_CONFIGURED = "not_configured"
    CHECKING = "checking"
    CHECK_UNAVAILABLE = "check_unavailable"
    UNREACHABLE = "unreachable"
    TIMED_OUT = "timed_out"
    SIGN_IN_REQUIRED = "sign_in_required"
    LIBRARY_UNAVAILABLE = "library_unavailable"
    SERVER_ERROR = "server_error"
    CONNECTED = "connected"


@dataclass(frozen=True)
class ConnectionHealth:
    state: ConnectionState
    username: str = ""
    checked_at: datetime | None = None
    library_name: str = ""


def check_connection(config: AppConfig) -> ConnectionHealth:
    try:
        client = client_from_config(config)
        user = client.get_current_user()
    except (AudiobookshelfError, OSError, ValueError) as error:
        return ConnectionHealth(connection_error_state(error))

    user = user.get("user", user)
    username = user.get("username") if isinstance(user, dict) else None
    if not isinstance(username, str) or not username.strip():
        return ConnectionHealth(ConnectionState.SERVER_ERROR)
    username = username.strip()

    try:
        libraries = client.get_libraries()
    except (AudiobookshelfError, OSError, ValueError) as error:
        state = connection_error_state(error, checking_library=True)
        return ConnectionHealth(state, "" if state == ConnectionState.SIGN_IN_REQUIRED else username)

    accessible_ids = {str(library["id"]) for library in libraries if library.get("id")}
    available = config.library_id in accessible_ids if config.library_id else bool(accessible_ids)
    if available and config.username.strip().casefold() in ("", "unknown user"):
        try:
            remember_verified_username(config.server_url, client.token, username)
        except OSError:
            print("Could not save the verified Audiobookshelf username.")
    state = ConnectionState.CONNECTED if available else ConnectionState.LIBRARY_UNAVAILABLE
    library_name = next(
        (str(library["name"]) for library in libraries
         if str(library.get("id")) == config.library_id and library.get("name")),
        "",
    )
    return ConnectionHealth(state, username, library_name=library_name)


def connection_error_state(error: Exception, *, checking_library: bool = False) -> ConnectionState:
    cause = error.__cause__ or error
    if isinstance(cause, HTTPError):
        if cause.code == 401:
            return ConnectionState.SIGN_IN_REQUIRED
        if checking_library and cause.code in (403, 404):
            return ConnectionState.LIBRARY_UNAVAILABLE
        if cause.code == 403:
            return ConnectionState.SIGN_IN_REQUIRED
    elif isinstance(cause, TimeoutError) or (
        isinstance(cause, URLError) and isinstance(cause.reason, TimeoutError)
    ):
        return ConnectionState.TIMED_OUT
    elif isinstance(cause, OSError):
        return ConnectionState.UNREACHABLE
    return ConnectionState.SERVER_ERROR


class ConnectionMonitor:
    def __init__(
        self,
        check: Callable[[AppConfig], ConnectionHealth] = check_connection,
        *,
        refresh_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._check = check
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._key: tuple[str, str, str, str] | None = None
        self._status = ConnectionHealth(ConnectionState.NOT_CONFIGURED)
        self._refresh_at = 0.0
        self._checking = False

    def status_for(self, config: AppConfig) -> ConnectionHealth:
        with self._lock:
            key = (
                (config.server_url, config.token, config.refresh_token, config.library_id)
                if config.is_ready
                else None
            )
            if key != self._key:
                self._key = key
                self._status = ConnectionHealth(
                    ConnectionState.CHECKING if key else ConnectionState.NOT_CONFIGURED
                )
                self._refresh_at = 0.0
            if key and not self._checking and self._clock() >= self._refresh_at:
                self._checking = True
                try:
                    threading.Thread(target=self._run_check, args=(config, key), daemon=True).start()
                except RuntimeError:
                    self._checking = False
                    self._refresh_at = self._clock() + self._refresh_seconds
                    self._status = ConnectionHealth(ConnectionState.CHECK_UNAVAILABLE)
            return self._status

    def _run_check(self, config: AppConfig, key: tuple[str, str, str, str]) -> None:
        result = ConnectionHealth(ConnectionState.SERVER_ERROR)
        try:
            result = self._check(config)
        finally:
            with self._lock:
                if key == self._key:
                    self._status = replace(result, checked_at=datetime.now().astimezone())
                    self._refresh_at = self._clock() + self._refresh_seconds
                self._checking = False
