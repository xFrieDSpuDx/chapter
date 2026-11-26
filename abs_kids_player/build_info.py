"""Identify the installed source bundle without requiring Git on the player."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import __version__


BUILD_SUFFIXES = frozenset({".py", ".html", ".css", ".js", ".png", ".svg"})


@dataclass(frozen=True)
class BuildInfo:
    version: str
    build_id: str | None


def source_fingerprint(package: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        files = sorted(
            path for path in package.rglob("*")
            if path.suffix in BUILD_SUFFIXES and path.is_file()
        )
        if not files:
            return None
        for path in files:
            digest.update(path.relative_to(package).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    except OSError:
        return None
    return digest.hexdigest()[:12]


@lru_cache(maxsize=1)
def installed_build() -> BuildInfo:
    return BuildInfo(__version__, source_fingerprint(Path(__file__).resolve().parent))


def log_installed_build(service: str) -> None:
    build = installed_build()
    print(f"Chapter {service} v{build.version} build={build.build_id or 'unavailable'}")
