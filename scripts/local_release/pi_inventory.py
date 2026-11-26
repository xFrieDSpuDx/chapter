"""Read Chapter's running service paths and inventory its downloads before a migration."""

import hashlib
import json
import pwd
import shlex
import stat
import subprocess
import sys
from pathlib import Path


def package_id(directory):
    package = directory / "abs_kids_player"
    suffixes = {".py", ".html", ".css", ".js", ".png", ".svg"}
    files = sorted(p for p in package.rglob("*") if p.suffix in suffixes and p.is_file())
    if not files:
        raise ValueError("Chapter package was not found at the running working directory")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(package).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:12]


def download_inventory(state):
    if not state.is_dir():
        raise ValueError("state directory missing or inaccessible")
    root = state / "downloads"
    if not root.exists():
        return {"download_directory": "absent"}
    completed, incomplete, invalid = [], 0, 0
    for account in sorted(root.iterdir()):
        if not account.is_dir():
            continue
        for directory in sorted(account.iterdir()):
            if not directory.is_dir():
                continue
            try:
                book = json.loads((directory / "book.json").read_bytes())
                tracks = book["tracks"]
                if not isinstance(tracks, list) or not tracks:
                    raise ValueError
                sizes = []
                complete = book["complete"] is True
                for index, track in enumerate(tracks, 1):
                    name = f"track-{index}"
                    size = track["size_bytes"]
                    if track["url"] != name or type(size) is not int or size <= 0:
                        raise ValueError
                    try:
                        actual = (directory / name).stat()
                        complete &= stat.S_ISREG(actual.st_mode) and actual.st_size == size
                    except FileNotFoundError:
                        complete = False
                    sizes.append(size)
                if complete:
                    completed.append((account.name, directory.name, sizes))
                else:
                    incomplete += 1
            except (FileNotFoundError, KeyError, TypeError, ValueError):
                invalid += 1
    return {
        "complete_books": len(completed),
        "incomplete_books": incomplete,
        "missing_or_invalid_records": invalid,
        "complete_track_bytes": sum(sum(sizes) for _, _, sizes in completed),
        "book_ids_and_track_sizes_sha256": hashlib.sha256(
            json.dumps(completed).encode()
        ).hexdigest(),
    }


def read_as_player(operation, path):
    # Chapter's data directory is private. Run the two file readers as the service account.
    subprocess.run(
        ["sudo", "-u", "chapter", "-H", "/usr/bin/python3", "-IB", "-", operation, str(path)],
        input=Path(__file__).read_bytes(),
        check=True,
    )


def inspect_pi():
    print("Boot ID", Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    states = set()
    home = Path(pwd.getpwnam("chapter").pw_dir)
    for name in ("oled", "setup", "port80-forward", "boot-splash", "update", "update-recovery"):
        unit = f"audiobookshelf-player-{name}.service"
        fields = (
            "LoadState,UnitFileState,ActiveState,FragmentPath,DropInPaths,"
            "WorkingDirectory,MainPID,User,EnvironmentFiles,Environment"
        )
        result = subprocess.run(
            ["systemctl", "--no-pager", "show", unit, "--property=" + fields],
            capture_output=True,
            text=True,
        )
        props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        print("\n" + unit, flush=True)
        if not props or props.get("LoadState") == "not-found":
            if name in ("oled", "setup"):
                raise ValueError(f"could not inspect required unit {unit}")
            print("  Unit missing or unavailable")
            continue
        for key in fields.split(",")[:-1]:
            if props.get(key):
                print(f"  {key}={props[key]}")
        if name not in ("oled", "setup") or props.get("LoadState") != "loaded":
            continue
        env = dict(v.split("=", 1) for v in shlex.split(props.get("Environment", "")) if "=" in v)
        user = props.get("User") or "root"
        if env.get("ABS_KIDS_PLAYER_STATE_PATH"):
            state = Path(env["ABS_KIDS_PLAYER_STATE_PATH"]).parent
        elif env.get("ABS_KIDS_PLAYER_STATE_DIR"):
            state = Path(env["ABS_KIDS_PLAYER_STATE_DIR"])
        else:
            state = (
                Path(env.get("HOME") or pwd.getpwnam(user).pw_dir) / ".local/state/abs-kids-player"
            )
        if props.get("EnvironmentFiles"):
            print("  Check EnvironmentFiles for a storage override before using this inventory.")
        elif state.is_absolute():
            print(f"  Configured state directory={state}", flush=True)
            states.add(state)
        else:
            print("  Relative state directory needs manual verification.")
        pid = props.get("MainPID", "0")
        if pid.isdigit() and int(pid) > 0:
            cwd = subprocess.check_output(
                ["sudo", "/usr/bin/readlink", "--", f"/proc/{pid}/cwd"],
                text=True,
            ).rstrip("\n")
            print(f"  Running working directory={cwd}", flush=True)
            source = (
                props["WorkingDirectory"]
                if name == "oled" and cwd == "/run/chapter-oled-gpio"
                else cwd
            )
            print(f"  Chapter source directory={source}", flush=True)
            read_as_player("package", source)
    for state in sorted(states):
        print(f"\nDownload inventory for {state}", flush=True)
        read_as_player("downloads", state)
    print("\nSpace on the Chapter home filesystem", flush=True)
    subprocess.run(["df", "-h", str(home)], check=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 1:
            inspect_pi()
        elif len(sys.argv) == 3 and sys.argv[1] == "package":
            print("  Package on disk at that path=" + package_id(Path(sys.argv[2])))
        elif len(sys.argv) == 3 and sys.argv[1] == "downloads":
            print(json.dumps(download_inventory(Path(sys.argv[2])), indent=2))
        else:
            sys.exit("Usage: python3 -IB chapter-pi-inventory.py")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sys.exit(f"Inventory incomplete: {error}")
