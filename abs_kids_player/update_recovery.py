"""Retain the installed deployment and verify Chapter's services after a restart."""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from http.client import HTTPException
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


OLED_SERVICE = "audiobookshelf-player-oled.service"
SETUP_SERVICE = "audiobookshelf-player-setup.service"
PLAYER_SERVICES = (SETUP_SERVICE, OLED_SERVICE)
PREVIOUS_RELEASE_REF = "refs/chapter/previous-release"
RECOVERY_SERVICE = "audiobookshelf-player-update-recovery.service"
RECOVERY_PROGRAM = Path("/usr/local/sbin/chapter-update-recovery")
SERVICE_HEALTH_TIMEOUT = 60.0
SERVICE_STABILITY_SECONDS = 5.0

INSTALLER_FILES = tuple(Path("/etc/systemd/system") / f"audiobookshelf-player-{name}.service" for name in (
    "boot-splash", "oled", "setup", "bluetooth-unblock", "update", "update-recovery", "port80-forward",
)) + (
    Path("/usr/local/sbin/audiobookshelf-player-port80-proxy"),
    RECOVERY_PROGRAM,
    Path("/etc/NetworkManager/dnsmasq-shared.d/audiobookshelf-player-captive-portal.conf"),
    Path("/etc/asound.conf"),
)


def installer_files() -> tuple[Path, ...]:
    boot_override = os.environ.get("CHAPTER_BOOT_CONFIG")
    boot_files = (Path(boot_override),) if boot_override else (
        Path("/boot/firmware/config.txt"), Path("/boot/config.txt"),
    )
    return INSTALLER_FILES + boot_files


class DeploymentBackup:
    def __init__(self, directory: Path, paths: Iterable[Path]) -> None:
        self.files = {}
        directory.mkdir(exist_ok=True)
        for index, path in enumerate(paths):
            snapshot = directory / str(index)
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                self.files[path] = None
                continue
            shutil.copy2(path, snapshot, follow_symlinks=False)
            self.files[path] = (snapshot, metadata.st_uid, metadata.st_gid)
            if not snapshot.is_symlink():
                sync_path(snapshot)

    def manifest(self) -> dict:
        data = {}
        for path, saved in self.files.items():
            if saved is None:
                data[str(path)] = None
            else:
                snapshot, uid, gid = saved
                data[str(path)] = [snapshot.name, uid, gid]
        return data

    @classmethod
    def from_manifest(cls, directory: Path, files: dict) -> DeploymentBackup:
        backup = cls(directory, ())
        for name, saved in files.items():
            path = Path(name)
            if not path.is_absolute() or (saved is not None and not str(saved[0]).isdigit()):
                raise ValueError("Invalid deployment backup path")
            backup.files[path] = (directory / saved[0], saved[1], saved[2]) if saved else None
        return backup

    def restore(self) -> None:
        for original, saved in self.files.items():
            if saved is None:
                original.unlink(missing_ok=True)
                if original.parent.is_dir():
                    sync_path(original.parent)
                continue
            snapshot, uid, gid = saved
            with tempfile.TemporaryDirectory(prefix=".chapter-restore-", dir=original.parent) as directory:
                restored = Path(directory) / original.name
                shutil.copy2(snapshot, restored, follow_symlinks=False)
                if restored.lstat().st_uid != uid or restored.lstat().st_gid != gid:
                    os.chown(restored, uid, gid, follow_symlinks=False)
                if not restored.is_symlink():
                    sync_path(restored)
                restored.replace(original)
            sync_path(original.parent)


def sync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def recovery_directory() -> Path:
    return Path(os.environ.get("CHAPTER_UPDATE_RECOVERY_DIR", "/var/lib/chapter-update"))


@contextmanager
def recovery_lock(root: Path):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "update.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def require_managed_checkout(directory: Path) -> None:
    marker = directory / ".git" / "chapter-managed"
    if not directory.is_absolute() or not marker.is_file():
        raise RuntimeError("Install a root-owned managed release before using online updates.")
    for path in (marker, *marker.parents):
        info = path.lstat()
        if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("The release and its parent directories must be protected by root.")


def command_for_install_user(args: list[str], install_user: str) -> list[str]:
    if os.geteuid() != 0:
        return args
    account = pwd.getpwnam(install_user)
    return ["runuser", "-u", install_user, "--", "env", f"HOME={account.pw_dir}", *args]


def retain_deployment(root: Path, paths: Iterable[Path], **release: str) -> Path:
    journal = root / "pending"
    if journal.exists():
        raise RuntimeError("A previous update still needs recovery. Restart Chapter to restore the installed build.")
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=root) as temporary:
        staged = Path(temporary) / "pending"
        staged.mkdir()
        backup = DeploymentBackup(staged / "files", paths)
        manifest = staged / "manifest.json"
        manifest.write_text(json.dumps({**release, "files": backup.manifest()}), encoding="utf-8")
        sync_path(manifest)
        sync_path(staged / "files")
        sync_path(staged)
        staged.rename(journal)
        sync_path(root)
    return journal


def restore_deployment(journal: Path, runner: Callable, *, after_reboot: bool = False) -> dict:
    state = json.loads((journal / "manifest.json").read_text(encoding="utf-8"))
    commit = state["previous_commit"]
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", commit):
        raise ValueError("Invalid previous release commit")
    git = ["git", "-C", state["install_dir"]]
    user = state["install_user"]
    root_checkout = state.get("checkout_owner") == "root"
    if root_checkout:
        require_managed_checkout(Path(state["install_dir"]))

    def checkout_command(args):
        return args if root_checkout else command_for_install_user(args, user)

    if after_reboot:
        # The installer cannot still hold Git locks after a reboot.
        location = runner(checkout_command([*git, "rev-parse", "--absolute-git-dir"]), timeout=10)
        git_dir = Path(location.stdout.strip())
        if not git_dir.is_absolute():
            raise RuntimeError("Could not locate the installed Git directory")
        locks = [str(git_dir / name) for name in ("index.lock", "HEAD.lock", "shallow.lock", "FETCH_HEAD.lock")]
        runner(checkout_command(["rm", "-f", "--", *locks]), timeout=10)
    runner(checkout_command([*git, "checkout", "--force", "--detach", commit]), timeout=30)
    # git may be using buffered writes. Finish flushing the restored checkout before clearing the recovery record.
    os.sync()
    DeploymentBackup.from_manifest(journal / "files", state["files"]).restore()
    runner(["systemctl", "daemon-reload"], timeout=30)
    return state


def discard_journal(journal: Path) -> None:
    with tempfile.TemporaryDirectory(prefix=".finished-", dir=journal.parent) as temporary:
        journal.rename(Path(temporary) / "retired")
        sync_path(journal.parent)


def record_boot_recovery(state: dict, runner: Callable) -> None:
    # Write the user-owned status as the install user so its path cannot redirect a privileged write.
    program = """
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
    temporary = Path(stream.name)
    stream.write(sys.argv[2].encode())
    stream.flush()
    os.fsync(stream.fileno())
try:
    temporary.replace(path)
finally:
    temporary.unlink(missing_ok=True)
"""
    status = json.dumps({
        "phase": "failed",
        "target_version": state["target_version"],
        "message": f"Update interrupted. Restored the previous build (v{state['previous_version']}).",
        "updated_at": time.time(),
    })
    runner(command_for_install_user(
        [sys.executable, "-c", program, state["state_file"], status], state["install_user"],
    ), timeout=10)


def recover_interrupted_update(root: Path, runner: Callable) -> bool:
    with recovery_lock(root) as acquired:
        journal = root / "pending"
        if not acquired or not journal.is_dir():
            return False
        state = restore_deployment(journal, runner, after_reboot=True)
        record_boot_recovery(state, runner)
        discard_journal(journal)
        return True


def service_identity(unit: str, runner: Callable) -> tuple[int, int] | None:
    output = runner(
        ["systemctl", "show", "-p", "ActiveState", "-p", "MainPID", "-p", "NRestarts", unit],
        timeout=5,
    ).stdout
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if fields.get("ActiveState") != "active":
        return None
    try:
        pid, restarts = int(fields["MainPID"]), int(fields["NRestarts"])
    except (KeyError, ValueError):
        return None
    return (pid, restarts) if pid > 0 else None


def setup_reports_fresh_display(version: str, restarted_at: float) -> bool:
    # The normal setup endpoint works locally even when Chapter is offline or signed out.
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:47831/player/status", timeout=1.0) as response:
            if response.headers.get("Server", "").split(" ", 1)[0] != f"ChapterPlayer/{version}":
                return False
            status = json.loads(response.read(16_384))
        if not isinstance(status, dict):
            return False
        now = time.time()
        drawn_at = float(status.get("displayDrawnAt", status.get("updatedAt", 0)))
        return max(restarted_at, now - 3) <= drawn_at <= now + 1
    except (OSError, HTTPException, TypeError, ValueError):
        return False


def wait_for_player_services(
    version: str,
    restarted_at: float,
    runner: Callable,
    *,
    timeout: float = SERVICE_HEALTH_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    reports_fresh_display: Callable[[str, float], bool] = setup_reports_fresh_display,
) -> None:
    deadline = clock() + timeout
    previous = None
    stable_since = None
    reason = "the player services did not become active"
    while True:
        identities = tuple(service_identity(unit, runner) for unit in PLAYER_SERVICES)
        failed = [unit for unit, identity in zip(PLAYER_SERVICES, identities) if identity is None]
        if failed:
            reason = "inactive service: " + ", ".join(failed)
            stable_since = None
        elif not reports_fresh_display(version, restarted_at):
            reason = "setup did not report a fresh OLED status for the installed version"
            stable_since = None
        elif stable_since is None or identities != previous:
            reason = "the OLED and setup services did not stay running"
            stable_since = clock()
        elif clock() - stable_since >= SERVICE_STABILITY_SECONDS:
            return
        previous = identities
        remaining = deadline - clock()
        if remaining <= 0:
            raise RuntimeError(f"Health check failed: {reason}.")
        sleep(min(1.0, remaining))


def main() -> None:
    def run(args: list[str], **options):
        return subprocess.run(args, check=True, capture_output=True, text=True, **options)

    if recover_interrupted_update(recovery_directory(), run):
        print("Restored the previous Chapter build after an interrupted update.", flush=True)


if __name__ == "__main__":
    main()
