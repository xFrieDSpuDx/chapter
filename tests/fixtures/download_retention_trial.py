"""Run one phase of the release check in a fresh Chapter process and a disposable home."""

import io
import json
import os
import sys
import wave
from email.message import Message
from functools import partial
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import unquote, urlsplit
from urllib.response import addinfourl

import abs_kids_player
from abs_kids_player.config import AppConfig, load_config, save_config
from abs_kids_player.download_store import books_with_downloads
from abs_kids_player.downloads import Downloads
from abs_kids_player.models import Book
from abs_kids_player.playback_start import prepare_book_playback


def audio_file(item):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 1, 8000, 0, "NONE", ""))
        sample = 120 if item == "first" else 128
        pattern = bytes(sample + index % 5 for index in range(251))
        audio.writeframes((pattern * (480_000 // len(pattern) + 1))[:480_000])
    return output.getvalue()


class InterruptedAudio(io.BytesIO):
    def read(self, size=-1):
        if self.tell():
            # Terminate the fixture between chunks, without Python closing its files.
            os._exit(73)
        return super().read(size)


def open_test_request(phase, request, timeout):
    path = urlsplit(request.full_url).path
    assert urlsplit(request.full_url).hostname == "shelf.invalid"
    item = path.split("/")[3]
    assert item in ("first", "second")
    data = audio_file(item)
    headers = Message()
    status = 200
    if path == f"/api/items/{item}":
        stream = io.BytesIO(json.dumps({"media": {
            "tracks": [{"ino": 13, "duration": 60, "metadata": {"size": len(data)}}],
            "metadata": {"description": item},
        }}).encode())
    elif path.endswith("/cover"):
        stream = io.BytesIO()
    else:
        assert path == f"/api/items/{item}/file/13/download"
        headers["ETag"] = '"edition-1"'
        byte_range = request.get_header("Range")
        if phase == "seed" and item == "second":
            assert byte_range is None
            stream = InterruptedAudio(data)
        elif phase == "resume":
            assert item == "second"
            offset = int(byte_range.removeprefix("bytes=").removesuffix("-"))
            assert 0 < offset < len(data) and request.get_header("If-range") == headers["ETag"]
            status = 206
            headers["Content-Range"] = f"bytes {offset}-{len(data)-1}/{len(data)}"
            stream = io.BytesIO(data[offset:])
        else:
            assert item == "first" and byte_range is None
            stream = io.BytesIO(data)
    headers["Content-Length"] = str(stream.getbuffer().nbytes)
    return addinfourl(stream, headers, request.full_url, status)


def inspect_offline(config, expected):
    queue = Downloads()
    assert [b.id for b in queue.queued_books(config)] == ([] if len(expected) == 2 else ["second"])
    assert {book.id for book in books_with_downloads(config, [])} == set(expected)
    for item in expected:
        def offline(request, timeout):
            assert request.get_method() == "GET"
            assert urlsplit(request.full_url).path == f"/api/me/progress/{item}"
            raise URLError("offline")

        with patch("abs_kids_player.api.urlopen", side_effect=offline):
            session = prepare_book_playback(item, config, local_only=True).session
        assert session.downloaded and len(session.tracks) == 1
        location = urlsplit(session.tracks[0].url)
        assert location.scheme == "file"
        track = Path(unquote(location.path))
        assert track.read_bytes() == audio_file(item), f"Audio differs for {item}"
        assert json.loads((track.parent / "details.json").read_text())["description"] == item


if __name__ == "__main__":
    phase = sys.argv[1]
    # The parent selects the build and always supplies a disposable home.
    assert Path(abs_kids_player.__file__).resolve().parent.parent == Path.cwd()
    assert (Path.home() / ".chapter-test-home").exists()
    if phase == "seed":
        config = AppConfig(server_url="https://shelf.invalid", username="test-reader",
                           token="fixture", library_id="books", screen_saver_clock_format="24-hour")
        save_config(config)
        queue = Downloads()
        for item in ("first", "second"):
            queue.start(config, Book(item, item, "Reader", 60, ""))
        with patch("abs_kids_player.api.urlopen", side_effect=partial(open_test_request, phase)):
            assert queue.process_next() and queue.process_next()
        raise AssertionError("Fixture did not interrupt the second transfer")
    config = load_config()
    assert config.screen_saver_clock_format == "24-hour"
    if phase == "resume":
        queue = Downloads()
        if queue.queued_books(config):
            with patch("abs_kids_player.api.urlopen", side_effect=partial(open_test_request, phase)):
                assert queue.process_next()
        inspect_offline(config, ["first", "second"])
    else:
        assert phase in ("offline", "verify")
        inspect_offline(config, ["first"] if phase == "offline" else ["first", "second"])
