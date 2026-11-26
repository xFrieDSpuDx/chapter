"""HTTP responses and per-request error recovery for the Chapter setup server."""

import errno
import json
import logging
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs

from .config import ConfigReadError
from .setup_views import render_request_error


logger = logging.getLogger(__name__)
MAX_POST_BODY_BYTES = 64 * 1024


def one(fields: dict[str, list[str]], name: str) -> str:
    return fields.get(name, [""])[0].strip()


class SetupHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._serve(self.handle_get)

    def do_POST(self) -> None:
        self._post_fields = None
        self._serve(self.handle_post)

    def handle_get(self) -> None:
        raise NotImplementedError

    def handle_post(self) -> None:
        raise NotImplementedError

    def post_fields(self) -> dict[str, list[str]] | None:
        cached = getattr(self, "_post_fields", None)
        if cached is not None:
            return cached
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self.close_connection = True
            self.send_request_error(400, "Invalid request length.")
            return None
        if length < 0:
            self.close_connection = True
            self.send_request_error(400, "Invalid request length.")
            return None
        if length > MAX_POST_BODY_BYTES:
            self.close_connection = True
            self.send_request_error(413, "Request is too large.")
            return None
        raw = self.rfile.read(length)
        if len(raw) != length:
            self.close_connection = True
            self.send_request_error(400, "The form was interrupted. Submit it again.")
            return None
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.send_request_error(400, "Request must use UTF-8 text.")
            return None
        fields = parse_qs(body, keep_blank_values=True)
        self._post_fields = fields
        return fields

    def _serve(self, route: Callable[[], None]) -> None:
        self._response_started = False
        try:
            route()
        except Exception as error:
            self.close_connection = True
            if self._response_started and isinstance(error, (ConnectionError, TimeoutError)):
                return
            if isinstance(error, OSError):
                logger.warning("Setup I/O failed (%s, errno %s)", type(error).__name__, error.errno)
                status = 503
                if isinstance(error, ConfigReadError):
                    message = str(error)
                elif error.errno == errno.ENOSPC:
                    message = "Chapter is out of storage space. Free some space and check the page again."
                else:
                    message = "Chapter could not complete the request. Check the page again in a moment."
            else:
                logger.exception("Setup request failed")
                status = 500
                message = "Something went wrong. Reopen the page to check its current state."
            if self._response_started:
                return
            # BaseHTTPRequestHandler buffers the status line and headers until end_headers().
            self._headers_buffer = []
            # An early failure can leave unread POST data. Start the next request on a fresh connection.
            try:
                self.send_request_error(status, message)
            except OSError:
                pass  # The browser may have left before the error could be sent.

    def end_headers(self) -> None:
        self._response_started = True
        super().end_headers()

    def send_request_error(self, status: int, message: str) -> None:
        headers = getattr(self, "headers", {})
        if headers.get("X-Requested-With") == "fetch" or "application/json" in headers.get("Accept", ""):
            self.send_json({"error": message}, status=status)
        else:
            self.send_html(status, render_request_error(message))

    def send_html(self, status: int, html_text: str) -> None:
        self._send_body(status, html_text.encode("utf-8"), "text/html; charset=utf-8", no_store=True)

    def send_text(self, text: str) -> None:
        self._send_body(200, text.encode("utf-8"), "text/plain; charset=utf-8")

    def send_json(self, data: dict, *, status: int = 200, attachment: str = "") -> None:
        body = json.dumps(data).encode("utf-8")
        self._send_body(status, body, "application/json; charset=utf-8", no_store=True, attachment=attachment)

    def _send_body(
        self, status: int, body: bytes, content_type: str, *, no_store: bool = False, attachment: str = "",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if no_store:
            self.send_header("Cache-Control", "no-store")
        if getattr(self, "close_connection", False):
            self.send_header("Connection", "close")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
