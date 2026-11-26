from __future__ import annotations

import json
import os
import pwd
import tempfile
from pathlib import Path


STORAGE_OWNER_ENV = "ABS_KIDS_PLAYER_STORAGE_OWNER"


def ensure_storage_directory(path: Path, *, shared_owner: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    if shared_owner:
        apply_configured_owner(path)


def finalize_storage_file(path: Path, mode: int = 0o600, *, shared_owner: bool = True) -> None:
    path.chmod(mode)
    if shared_owner:
        apply_configured_owner(path)


def write_json_atomically(
    path: Path,
    data: dict | list,
    *,
    compact: bool = False,
    durable: bool = False,
    shared_owner: bool = True,
) -> None:
    ensure_storage_directory(path.parent, shared_owner=shared_owner)
    stream = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    temporary_path = Path(stream.name)
    try:
        with stream:
            if compact:
                json.dump(data, stream, separators=(",", ":"))
            else:
                json.dump(data, stream, indent=2)
            if durable:
                stream.flush()
                os.fsync(stream.fileno())
        finalize_storage_file(temporary_path, shared_owner=shared_owner)
        temporary_path.replace(path)
        if durable:
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)


def apply_configured_owner(path: Path) -> None:
    owner = configured_storage_owner()
    if owner is None:
        return
    os.chown(path, owner.pw_uid, owner.pw_gid)


def configured_storage_owner() -> pwd.struct_passwd | None:
    username = os.environ.get(STORAGE_OWNER_ENV, "").strip()
    if not username or os.geteuid() != 0:
        return None
    return pwd.getpwnam(username)
