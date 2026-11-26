"""Validate and transfer an audiobook’s original audio files."""

from __future__ import annotations

import math
import os
import shutil
import threading
from dataclasses import replace
from http.client import HTTPMessage, HTTPResponse
from pathlib import Path
from urllib.parse import quote

from .api import AudiobookshelfClient, client_from_config, parse_chapters
from .config import AppConfig
from .download_assets import save_details, save_download_cover, select_details
from .download_file_usage import download_file_usage
from .download_store import DownloadedBook, download_root, file_size, lock_download, read_download
from .models import AudioTrack, Book, Chapter
from .storage import ensure_storage_directory, finalize_storage_file


MIB = 1024 * 1024
DISK_RESERVE_BYTES = 256 * MIB
CHUNK_BYTES = 256 * 1024


class DownloadError(RuntimeError):
    pass


class DownloadCancelled(DownloadError):
    pass


class DownloadInterrupted(DownloadError):
    pass


def check_cancelled(cancelled: threading.Event) -> None:
    if cancelled.is_set():
        raise DownloadCancelled("Cancelled. Retry to continue.")


def storage_usage() -> tuple[int, int]:
    root = download_root()
    used = sum(download_file_usage(path).total_bytes for path in root.glob("*/*/"))
    parent = next(path for path in (root, *root.parents) if path.is_dir())
    return used, shutil.disk_usage(parent).free


def download_metadata(client: AudiobookshelfClient, book: Book) -> tuple[DownloadedBook, dict]:
    item = client.request("GET", f"/api/items/{quote(book.id, safe='')}", query={"expanded": 1})
    if not isinstance(item, dict):
        raise DownloadError("The server did not return book metadata.")
    media = item.get("media") or {}
    raw_tracks = media.get("tracks")
    if not isinstance(raw_tracks, list):
        raw_tracks = [row for row in media.get("audioFiles", []) if not row.get("exclude")]
    tracks = []
    offset = 0.0
    for index, raw in enumerate(raw_tracks, 1):
        metadata = raw.get("metadata") or {}
        length = float(raw.get("duration") or 0)
        size = int(metadata.get("size") or 0)
        file_id = int(raw.get("ino") or 0)
        if not math.isfinite(length) or length <= 0 or not (size > 0 and file_id > 0):
            raise DownloadError("The server did not provide complete audio file metadata.")
        tracks.append(AudioTrack(index, offset, length, f"track-{index}", str(
            raw.get("title") or metadata.get("filename") or f"Track {index}"
        ), file_id, size))
        offset += length
    if not tracks:
        raise DownloadError("This book has no downloadable audio files.")
    chapters = parse_chapters(media.get("chapters") or [], offset) or [
        Chapter(track.index, track.title, track.start_offset, track.start_offset + track.duration)
        for track in tracks
    ]
    return DownloadedBook(replace(book, duration=offset, cover_url="", downloaded=False), tracks, chapters), select_details(item)


def _response_offset(response: HTTPResponse, offset: int, expected: int, validator: str) -> int:
    if response.status == 206:
        expected_range = f"bytes {offset}-{expected - 1}/{expected}"
        if not offset or response.headers.get("Content-Range") != expected_range:
            raise DownloadError("The server returned a different byte range. Remove and download the book again.")
        response_validator = response.headers.get("ETag" if validator.startswith('"') else "Last-Modified")
        if response_validator and response_validator != validator:
            raise DownloadError("The audio file changed. Remove and download the book again.")
    elif response.status == 200:
        offset = 0
    else:
        raise DownloadError("The server did not return an audio file.")
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) != expected - offset:
        raise DownloadError("The audio file changed. Remove and download the book again.")
    return offset


def _read_audio_block(response: HTTPResponse, remaining: int, cancelled: threading.Event) -> bytes:
    check_cancelled(cancelled)
    block = response.read(CHUNK_BYTES)
    check_cancelled(cancelled)
    if len(block) > remaining:
        raise DownloadError("The server sent more audio than expected.")
    return block


def _save_validator(path: Path, headers: HTTPMessage) -> None:
    etag = headers.get("ETag") or ""
    validator = etag if etag.startswith('"') else headers.get("Last-Modified") or ""
    with path.open("w", encoding="utf-8") as stream:
        finalize_storage_file(path)
        stream.write(validator)
        stream.flush()
        os.fsync(stream.fileno())


def transfer_track(
    client: AudiobookshelfClient, book_id: str, track: AudioTrack, directory: Path, cancelled: threading.Event
) -> None:
    check_cancelled(cancelled)
    destination = directory / track.url
    partial = destination.with_suffix(".part")
    validator_path = destination.with_suffix(".validator")
    expected = track.size_bytes or 0
    if file_size(destination) == expected:
        return
    partial_size = file_size(partial)
    offset = partial_size
    try:
        validator = validator_path.read_text(encoding="utf-8") if offset else ""
    except OSError:
        validator = ""
    if offset and (not validator or offset >= expected):
        offset = 0
    path = f"/api/items/{quote(book_id, safe='')}/file/{track.file_id}/download"
    with client.open_download(path, offset, validator=validator if offset else "") as response:
        offset = _response_offset(response, offset, expected, validator)
        block = _read_audio_block(response, expected - offset, cancelled)
        if not block:
            raise DownloadInterrupted("The audio transfer was interrupted.")
        with partial.open("ab" if offset else "wb") as output:
            finalize_storage_file(partial)
            if not offset:
                # Truncate the old audio before publishing the new file's validator.
                if partial_size:
                    os.fsync(output.fileno())
                _save_validator(validator_path, response.headers)
            while block:
                offset += len(block)
                output.write(block)
                block = _read_audio_block(response, expected - offset, cancelled)
            if offset != expected:
                raise DownloadInterrupted("The audio transfer was interrupted.")
            output.flush()
            os.fsync(output.fileno())
    partial.replace(destination)
    validator_path.unlink(missing_ok=True)


def transfer_book(config: AppConfig, book: Book, directory: Path, cancelled: threading.Event) -> None:
    check_cancelled(cancelled)
    previous = read_download(directory)
    if previous and previous.complete:
        return
    client = client_from_config(config)
    manifest, details = download_metadata(client, book)
    if previous and previous.tracks != manifest.tracks:
        raise DownloadError("The audio files changed. Remove and download the book again.")

    used, free = storage_usage()
    present = sum(file_size(path) for path in directory.glob("track-*"))
    needed = manifest.total_bytes - present
    if needed > config.download_limit_mib * MIB - used:
        raise DownloadError("Download storage limit reached.")
    if needed > free - DISK_RESERVE_BYTES:
        raise DownloadError("There is insufficient free space on the microSD card.")

    check_cancelled(cancelled)
    ensure_storage_directory(directory)
    with lock_download(directory, shared=False):
        manifest.save(directory)
        save_details(directory, details)
        for track in manifest.tracks:
            transfer_track(client, book.id, track, directory, cancelled)
        check_cancelled(cancelled)
        try:
            save_download_cover(client, directory, book.id)
        except OSError:
            # Audio remains available if optional artwork cannot be written.
            pass
        check_cancelled(cancelled)
        replace(manifest, complete=True).save(directory)
