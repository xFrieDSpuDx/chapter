"""Measure downloads without re-reading the size of every finished track."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from stat import S_ISREG

from .download_store import MANIFEST_NAME


@dataclass(frozen=True)
class FileUsage:
    audio_bytes: int = 0
    total_bytes: int = 0


@dataclass(frozen=True)
class DirectoryFiles:
    stable: FileUsage
    partials: tuple[Path, ...]
    validators: tuple[Path, ...]


def regular_file_size(path: Path) -> int:
    try:
        info = path.lstat()
    except OSError:
        return 0
    return info.st_size if S_ISREG(info.st_mode) else 0


@lru_cache(maxsize=512)
def directory_files(directory: Path, revision: tuple[int, int, int, int]) -> DirectoryFiles:
    audio = total = 0
    partials = []
    validators = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name.startswith("track-"):
                if entry.name.endswith(".part"):
                    partials.append(directory / entry.name)
                elif entry.name.endswith(".validator"):
                    validators.append(directory / entry.name)
                elif entry.is_file(follow_symlinks=False):
                    size = regular_file_size(directory / entry.name)
                    audio += size
                    total += size
            elif entry.name.startswith("cover.") or entry.name in ("details.json", MANIFEST_NAME):
                total += regular_file_size(directory / entry.name)
    return DirectoryFiles(FileUsage(audio, total), tuple(partials), tuple(validators))


def download_file_usage(directory: Path) -> FileUsage:
    try:
        info = directory.stat()
        files = directory_files(directory, (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns))
    except OSError:
        return FileUsage()

    # A transfer appends to the partial file in place. Directory changes cover the
    # other cases, including publishing a track or replacing the saved artwork.
    partial_bytes = sum(regular_file_size(path) for path in files.partials)
    validator_bytes = sum(regular_file_size(path) for path in files.validators)
    return FileUsage(files.stable.audio_bytes + partial_bytes,
                     files.stable.total_bytes + partial_bytes + validator_bytes)
