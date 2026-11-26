"""Stage an offline Debian package with the new runtime and the known working runtime."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from pi_inventory import package_id


PROXY = Path("deploy/audiobookshelf-player-port80-proxy")
SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent.parent


def allowed(path):
    return (
        bool(path.parts)
        and not path.is_absolute()
        and ".." not in path.parts
        and (
            path == PROXY
            or path.parts[0] == "abs_kids_player"
            and path.suffix
            in {
                ".py",
                ".html",
                ".js",
                ".css",
                ".png",
                ".svg",
            }
        )
    )


def stage_fallback(source, destination):
    if not source.is_dir():
        raise ValueError("Expected the extracted directory of the previous permanent Debian package")
    installed = json.loads((source / "release.json").read_bytes())
    if not re.fullmatch(r"chapter-local-[0-9a-f]{16}", source.name) or (
        source.name != installed["package"]
    ):
        raise ValueError("The previous package directory does not match its manifest")
    candidate = source / "candidate"
    for entry in candidate.rglob("*"):
        path = entry.relative_to(candidate)
        if entry.is_symlink():
            raise ValueError(f"Unexpected link in previous package: {path}")
        if entry.is_dir():
            continue
        if not entry.is_file() or not allowed(path):
            raise ValueError(f"Unexpected previous package entry: {path}")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(entry, target)
    if package_id(destination) != installed["candidate"]:
        raise ValueError("The previous player files differ from its manifest")


def stage_package(stage, fallback):
    stage.mkdir()
    payload = stage / "opt/chapter-player/payload"
    candidate = payload / "candidate"
    files = subprocess.check_output(
        [
            "git",
            "-C",
            str(REPO),
            "ls-files",
            "-co",
            "--exclude-standard",
            "--",
            "abs_kids_player",
            str(PROXY),
        ],
        text=True,
    ).splitlines()
    for name in files:
        path = Path(name)
        if not allowed(path) or not (REPO / path).is_file() or (REPO / path).is_symlink():
            raise ValueError(f"Unexpected runtime file: {name}")
        destination = candidate / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / path, destination)
    previous = payload / "fallback"
    stage_fallback(fallback, previous)
    origin = {"previous_package": fallback.name}
    for name in ("release.py", "pi_inventory.py", "pi_checks.py"):
        shutil.copyfile(SCRIPTS / name, payload / name)
    for build in (candidate, previous):
        for path in (
            PROXY,
            Path("abs_kids_player/oled_service.py"),
            Path("abs_kids_player/setup_server.py"),
        ):
            if not (build / path).is_file():
                raise ValueError(f"Missing runtime entry point: {path}")

    digest = hashlib.sha256()
    for path in sorted(p for p in payload.rglob("*") if p.is_file()):
        digest.update(path.relative_to(payload).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    digest.update(json.dumps(origin, sort_keys=True).encode())
    bundle_id = digest.hexdigest()[:16]
    package = "chapter-local-" + bundle_id
    manifest = {
        "package": package,
        "candidate": package_id(candidate),
        "fallback": package_id(previous),
        **origin,
    }
    (payload / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
    payload.rename(payload.with_name(package))
    control = stage / "DEBIAN"
    control.mkdir()
    (control / "control").write_text(
        f"Package: {package}\nVersion: 1\nArchitecture: all\nMaintainer: Chapter local build\n"
        "Description: Chapter Player offline release and rollback for an existing appliance\n"
    )
    files = sorted(p for p in (stage / "opt").rglob("*") if p.is_file())
    sums = [
        f"{hashlib.md5(p.read_bytes(), usedforsecurity=False).hexdigest()}  {p.relative_to(stage)}"
        for p in files
    ]
    (control / "md5sums").write_text("\n".join(sums) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", type=Path, help="A new packaging directory")
    parser.add_argument(
        "fallback", type=Path, help="Extracted directory of the previous permanent Debian package"
    )
    arguments = parser.parse_args()
    print(json.dumps(stage_package(arguments.stage, arguments.fallback), indent=2))
