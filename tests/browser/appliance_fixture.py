"""Serve the real web UI against a disposable catalogue and simulated playback device."""

import os
import secrets
import sys
import time
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from abs_kids_player import setup_server
from abs_kids_player.config import AppConfig, save_config
from abs_kids_player.library_cache import save_cached_books
from abs_kids_player.models import Book
from abs_kids_player.owner_auth import change_public_access, load_credential, set_password
from abs_kids_player.player_control import (
    WebPlayerStatus,
    consume_web_player_commands,
    save_command_result,
    save_web_player_status,
)
from abs_kids_player.progress_sync import progress_account
from abs_kids_player.wifi import WifiStatus


def simulate_device(config, books, closed):
    status = WebPlayerStatus(volume_percent=32)
    while not closed.wait(0.03):
        for command in consume_web_player_commands():
            if command.action == "play_book":
                book = books[command.book_id]
                status = replace(
                    status, title=book.title, author=book.author, duration=book.duration,
                    cover_item_id=book.id, session_key=command.id, library_id=config.library_id,
                    account=progress_account(config), is_playing=True, audio_output="speaker",
                )
            elif command.action == "volume":
                status = replace(status, volume_percent=command.value)
            elif command.action in ("play", "pause"):
                status = replace(status, is_playing=command.action == "play")
            elif command.action == "stop":
                status = WebPlayerStatus(volume_percent=status.volume_percent)
            else:
                save_command_result(command, "rejected")
                continue
            save_web_player_status(replace(status, updated_at=time.time()))
            save_command_result(command, "applied")


def main():
    directory = Path(sys.argv[1])
    os.environ.update(
        ABS_KIDS_PLAYER_CONFIG_PATH=str(directory / "config.json"),
        ABS_KIDS_PLAYER_STATE_DIR=str(directory),
        CHAPTER_OWNER_FILE=str(directory / "owner.json"),
        CHAPTER_RELEASE_SOURCE="local",
    )
    config = AppConfig(server_url="http://127.0.0.1:1", token="fixture", username="reader", library_id="books")
    save_config(config)
    books = [
        Book("river", "The River", "Alex Green", 3600, ""),
        Book("shore", "The Shore", "Alex Green", 5400, ""),
    ]
    save_cached_books(config, books)
    set_password(os.environ["CHAPTER_TEST_PASSWORD"], "reader")
    change_public_access(load_credential(), overview=False, library=True)
    closed = Event()
    device = Thread(target=simulate_device, args=(config, {book.id: book for book in books}, closed), daemon=True)
    with (
        setup_server.SetupServer(("127.0.0.1", 0), setup_server.SetupHandler) as server,
        patch.object(setup_server, "wifi_status", return_value=WifiStatus(True, ssid="Home")),
        patch.object(setup_server, "bluetooth_status", return_value=(False, "")),
        patch.object(setup_server, "book_metadata", return_value={"description": "A journey downstream."}),
        patch.object(setup_server, "bookmarks_for_item", return_value=[]),
    ):
        server.csrf_token = secrets.token_urlsafe()
        device.start()
        try:
            print(f"http://127.0.0.1:{server.server_port}", flush=True)
            server.serve_forever()
        finally:
            closed.set()
            device.join()


if __name__ == "__main__":
    main()
