"""Select a packaged release on an already provisioned Chapter appliance."""

import argparse
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


BUNDLE = Path(__file__).resolve().parent
CURRENT = Path("/opt/chapter-player/current")
RECORDS = Path("/var/lib/chapter-local-release")
SYSTEMD = Path("/etc/systemd/system")
STATE = Path("/home/chapter/.local/state/abs-kids-player")
CONFIG = Path("/home/chapter/.config/abs-kids-player/config.json")
OWNER = Path("/var/lib/chapter-owner/credential.json")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
PROXY = "audiobookshelf-player-port80-forward.service"
OLED = "audiobookshelf-player-oled.service"
WEB = "audiobookshelf-player-setup.service"
LIVE = (OLED, WEB, PROXY)
SPLASH = "audiobookshelf-player-boot-splash.service"
OLED_GPIO = Path("/run/chapter-oled-gpio")
SPLASH_GPIO = Path("/run/chapter-splash-gpio")
DROP_IN = "zzzzzzzzzzzz-chapter-local-release.conf"


def run(*args: str, timeout=60) -> str:
    return subprocess.check_output(args, text=True, timeout=timeout).strip()


def property_of(unit, key):
    return run("systemctl", "show", unit, "--property=" + key, "--value")


def process_directory(unit):
    pid = int(property_of(unit, "MainPID") or 0)
    if pid <= 0:
        raise RuntimeError(f"{unit} has no running process")
    return Path(os.readlink(f"/proc/{pid}/cwd"))


def running_source(unit):
    cwd = process_directory(unit)
    # lgpio changes the OLED process's cwd when it opens its notification pipe.
    if unit == OLED and cwd == OLED_GPIO:
        return Path(property_of(unit, "WorkingDirectory")).resolve()
    return cwd


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        staged = Path(directory) / "new"
        with staged.open("wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
        sync_directory(path.parent)
        sync_directory(path.parent.parent)


def select_release(target):
    with tempfile.TemporaryDirectory(dir=CURRENT.parent) as directory:
        link = Path(directory) / "current"
        link.symlink_to(target)
        os.replace(link, CURRENT)
        sync_directory(CURRENT.parent)


RECOVERY_UNIT = "chapter-local-recovery.service"


def enable_boot_recovery():
    before = " ".join((*LIVE, SPLASH, "sysinit.target"))
    atomic_write(
        SYSTEMD / RECOVERY_UNIT,
        (
            "[Unit]\nDescription=Recover an interrupted local Chapter installation\nDefaultDependencies=no\n"
            f"After=local-fs.target\nBefore={before}\nConditionPathExists={RECORDS}/pending.json\n"
            f"[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -IB {BUNDLE}/release.py recover\n"
            "[Install]\nWantedBy=sysinit.target\n"
        ).encode(),
    )
    run("systemctl", "enable", RECOVERY_UNIT)


def prepare_services():
    recovery = RECOVERY_UNIT
    ordering = f"[Unit]\nRequires={recovery}\nAfter={recovery}\n"
    local_env = "Environment=CHAPTER_RELEASE_SOURCE=local CHAPTER_LOCAL_TRIAL=1\n"
    python = "/usr/bin/python3 -EsBu"
    services = {
        OLED: (
            "-m abs_kids_player.oled_service --boot-splash-handoff-path=",
            "Environment=CHAPTER_PRELOAD_AUDIO_PLUGINS=1\n",
        ),
        WEB: ("-m abs_kids_player.setup_server --ensure-wifi", ""),
        PROXY: (f"{CURRENT}/deploy/audiobookshelf-player-port80-proxy --target-port 47831", ""),
        SPLASH: (
            "-m abs_kids_player.boot_splash --hold-seconds 0.1",
            f"Environment=ABS_KIDS_PLAYER_CONFIG_DIR={CONFIG.parent}\n",
        ),
    }
    for unit, (arguments, environment) in services.items():
        gpio = {OLED: OLED_GPIO, SPLASH: SPLASH_GPIO}.get(unit)
        if gpio:
            environment += f"RuntimeDirectory={gpio.name}\nRuntimeDirectoryMode=0700\nEnvironment=LG_WD={gpio}\n"
        data = (
            ordering
            + f"[Service]\nWorkingDirectory={CURRENT}\n"
            + local_env
            + environment
            + f"ExecStart=\nExecStart={python} {arguments}\nStandardOutput=journal\nStandardError=journal\n"
        )
        atomic_write(SYSTEMD / (unit + ".d") / DROP_IN, data.encode())
    # The Git updater targets published releases. Its old unit may still refer to the home checkout.
    atomic_write(
        SYSTEMD / "audiobookshelf-player-update.service.d" / DROP_IN,
        (
            f"[Unit]\nConditionPathExists=!{CURRENT}\n[Service]\nWorkingDirectory={CURRENT}\nExecStart=\nExecStart=/usr/bin/false\n"
        ).encode(),
    )
    run("systemctl", "daemon-reload")


def inventory():
    output = run(
        "runuser",
        "-u",
        "chapter",
        "--",
        "/usr/bin/python3",
        "-IB",
        str(BUNDLE / "pi_inventory.py"),
        "downloads",
        str(STATE),
    )
    return json.loads(output)


def check_storage_paths(unit):
    pid = int(property_of(unit, "MainPID"))
    values = dict(
        entry.split(b"=", 1)
        for entry in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        if b"=" in entry
    )
    home = Path(os.fsdecode(values.get(b"HOME", b"/root" if unit == WEB else b"/home/chapter")))
    for name, default, expected in (
        ("CONFIG", home / ".config/abs-kids-player", CONFIG),
        ("STATE", home / ".local/state/abs-kids-player", STATE / "player-state.json"),
    ):
        directory = Path(
            os.fsdecode(values.get(f"ABS_KIDS_PLAYER_{name}_DIR".encode(), os.fsencode(default)))
        )
        live = Path(
            os.fsdecode(
                values.get(
                    f"ABS_KIDS_PLAYER_{name}_PATH".encode(), os.fsencode(directory / expected.name)
                )
            )
        )
        if live != expected:
            raise RuntimeError(
                f"{unit} uses a different {name.lower()} location. Keep the current build active."
            )


def compatible_selection(target, manifest):
    if target in (BUNDLE / "candidate", BUNDLE / "fallback"):
        return True
    previous = target.parent
    if (
        target.name not in ("candidate", "fallback")
        or previous.parent != BUNDLE.parent
        or not re.fullmatch(r"chapter-local-[0-9a-f]{16}", previous.name)
    ):
        return False
    installed = json.loads((previous / "release.json").read_bytes())
    source_package = manifest.get("previous_package")
    previous_candidate = previous.name == source_package and target.name == "candidate"
    # A rolled-back upgrade runs that same accepted player from its fallback slot.
    rolled_back = (
        source_package
        and target.name == "fallback"
        and installed.get("previous_package") == source_package
    )
    if previous_candidate or rolled_back:
        if (
            installed.get("package") != previous.name
            or installed.get(target.name) != manifest["fallback"]
        ):
            return False
        # The player build ID covers the Python package. The port-80 proxy lives beside it.
        proxy = Path("deploy/audiobookshelf-player-port80-proxy")
        return (target / proxy).read_bytes() == (
            BUNDLE / "fallback" / proxy
        ).read_bytes() and not run("dpkg", "--verify", previous.name)
    # A packaging-only repair may also replace the manager of an identical release.
    return installed == {**manifest, "package": previous.name}


def check_baseline(manifest):
    selected = CURRENT.resolve() if CURRENT.is_symlink() else None
    if not selected and "previous_package" in manifest:
        raise RuntimeError("This upgrade requires the previous permanent package to be selected")
    if selected and not compatible_selection(selected, manifest):
        raise RuntimeError("A different local release is selected")
    for unit in LIVE:
        if property_of(unit, "UnitFileState") != "enabled":
            raise RuntimeError(f"{unit} must already be enabled on this appliance")
    for unit in (OLED, WEB):
        check_storage_paths(unit)
        running = running_source(unit)
        if selected and running == selected:
            continue
        if selected:
            raise RuntimeError(f"{unit} is using {running} instead of the selected {selected}")
        expected = Path("/home/chapter") / (
            "chapter-bluetooth-" + manifest["fallback_archive"][:12]
        )
        if running != expected:
            raise RuntimeError(
                f"{unit} is using {running}. The packaged fallback must match the active build."
            )
        actual = run(
            "runuser",
            "-u",
            "chapter",
            "--",
            "/usr/bin/python3",
            "-IB",
            str(BUNDLE / "pi_inventory.py"),
            "package",
            str(running),
        )
        if actual.rsplit("=", 1)[-1] != manifest["fallback"]:
            raise RuntimeError(f"The active {unit} files differ from the packaged fallback")
    if Path("/var/lib/chapter-update/pending").exists():
        raise RuntimeError("A published-release update is pending recovery")


def check_oled_startup(expected_build):
    invocation = property_of(OLED, "InvocationID")
    pid = property_of(OLED, "MainPID")
    if not re.fullmatch(r"[0-9a-f]{32}", invocation) or not pid.isdecimal() or int(pid) == 0:
        raise ValueError("waiting for the OLED service to start")
    try:
        records = run(
            "journalctl",
            "-b",
            "--quiet",
            "--no-pager",
            "--output=cat",
            f"_SYSTEMD_INVOCATION_ID={invocation}",
            f"_PID={pid}",
            "--grep=^(Chapter OLED v|Navigation rotary |Volume rotary )",
            "--lines=12",
        ).splitlines()
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
        records = []
    for dial in ("Navigation", "Volume"):
        failure = next(
            (line for line in records if line.startswith(f"{dial} rotary could not start:")), None
        )
        if failure:
            raise RotaryStartupError(failure)
        if not any(line.startswith(f"{dial} rotary enabled on ") for line in records):
            raise ValueError(f"waiting for {dial.lower()} dial startup")
    if not any(
        line.startswith("Chapter OLED v") and line.endswith(" build=" + expected_build)
        for line in records
    ):
        raise ValueError("waiting for the selected OLED build")


class RotaryStartupError(Exception):
    """A dial failed for this service invocation and requires a fresh start."""


def check_health(target, since):
    opener = build_opener(ProxyHandler({}))
    expected = json.loads((target.parent / "release.json").read_bytes())[target.name]
    deadline = time.monotonic() + 90
    detail = "services starting"
    while time.monotonic() < deadline:
        try:
            for unit in LIVE:
                if running_source(unit) != target:
                    raise ValueError(f"{unit} has a different running directory")
            for port in (47831, 80):
                with opener.open(f"http://127.0.0.1:{port}/player/status", timeout=2) as reply:
                    when = float(json.load(reply)["displayDrawnAt"])
                if not max(since, time.time() - 5) <= when <= time.time() + 1:
                    raise ValueError("waiting for a fresh OLED frame")
            check_oled_startup(expected)
            return
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
            detail = str(error)
            time.sleep(1)
    raise RuntimeError(f"Release health check timed out ({detail})")


def switch(target):
    run("systemctl", "stop", *LIVE, timeout=120)
    select_release(target)
    since = time.time()
    run("systemctl", "start", *LIVE, timeout=120)
    check_health(target, since)


def save_baseline():
    destination = RECORDS / "before.json"
    before = {"boot": BOOT_ID.read_text().strip(), "downloads": inventory()}
    if destination.exists():
        return before
    if CONFIG.exists():
        # Read the shared preferences with the same privileges as the player.
        settings = subprocess.check_output(["runuser", "-u", "chapter", "--", "cat", str(CONFIG)])
        atomic_write(RECORDS / "config.before.json", settings, mode=0o600)
    if OWNER.exists():
        atomic_write(RECORDS / "owner.before.json", OWNER.read_bytes(), mode=0o600)
    atomic_write(destination, json.dumps(before).encode())
    return before


def confirm_books(before):
    if inventory() != before["downloads"]:
        raise RuntimeError("The downloaded book inventory differs from the saved baseline")


def activate(manifest):
    check_baseline(manifest)
    if run("dpkg", "--verify", manifest["package"]):
        raise RuntimeError("Installed files differ from the local package")
    before = save_baseline()
    fallback = BUNDLE / "fallback"
    candidate = BUNDLE / "candidate"
    if not CURRENT.is_symlink():
        select_release(fallback)
    enable_boot_recovery()
    pending = RECORDS / "pending.json"
    atomic_write(pending, json.dumps({"boot": BOOT_ID.read_text().strip()}).encode())
    try:
        prepare_services()
        switch(candidate)
        confirm_books(before)
        atomic_write(RECORDS / "accepted.json", json.dumps(before).encode())
    except Exception as failure:
        prepare_services()
        switch(fallback)
        pending.unlink(missing_ok=True)
        sync_directory(RECORDS)
        raise RuntimeError(
            f"Activation failed. The previous player build was restored: {failure}"
        ) from failure
    pending.unlink()
    sync_directory(RECORDS)


def recover():
    pending = RECORDS / "pending.json"
    if pending.exists() and json.loads(pending.read_bytes())["boot"] != BOOT_ID.read_text().strip():
        select_release(BUNDLE / "fallback")
        prepare_services()
        pending.unlink()
        sync_directory(RECORDS)


def verify_boot():
    accepted = json.loads((RECORDS / "accepted.json").read_bytes())
    if accepted["boot"] == BOOT_ID.read_text().strip():
        raise RuntimeError("The Pi still has the same boot ID. Verify after a full reboot.")
    if CURRENT.resolve() != BUNDLE / "candidate":
        raise RuntimeError("The candidate is not the selected release")
    check_health(BUNDLE / "candidate", 0)
    confirm_books(accepted)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("activate", "rollback", "verify-boot", "recover"))
    action = parser.parse_args().action
    if os.geteuid() != 0:
        parser.exit(1, "Run this command with sudo.\n")
    if action == "recover":
        # During a live restart systemd may start this unit while activate holds the lock.
        recover()
        return
    RECORDS.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (RECORDS / "operation.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = json.loads((BUNDLE / "release.json").read_bytes())
        if action == "activate":
            activate(manifest)
        elif action == "rollback":
            if not (RECORDS / "before.json").is_file():
                raise RuntimeError("There is no local installation to roll back")
            prepare_services()
            switch(BUNDLE / "fallback")
            (RECORDS / "pending.json").unlink(missing_ok=True)
            sync_directory(RECORDS)
        else:
            verify_boot()
        print(
            f"{action} passed · running {CURRENT.resolve()} · package {manifest[CURRENT.resolve().name]}"
        )


if __name__ == "__main__":
    try:
        main()
    except (
        RuntimeError,
        RotaryStartupError,
        OSError,
        ValueError,
        KeyError,
        subprocess.SubprocessError,
    ) as error:
        raise SystemExit(str(error))
