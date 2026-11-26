from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import __version__
from .storage import write_json_atomically
from .update_recovery import (
    OLED_SERVICE,
    PLAYER_SERVICES,
    PREVIOUS_RELEASE_REF,
    SETUP_SERVICE,
    discard_journal,
    installer_files,
    recovery_directory,
    recovery_lock,
    require_managed_checkout,
    restore_deployment,
    retain_deployment,
    wait_for_player_services,
)


DEFAULT_REPO_URL = "https://github.com/chrismtopher/chapter.git"
DEFAULT_INSTALL_USER = "chapter"
DEFAULT_INSTALL_DIR = "/opt/chapter-player/git"
UPDATE_SERVICE_NAME = "audiobookshelf-player-update.service"
UPDATE_STATE_FILENAME = "software-update.json"
VERSION_NUMBER = r"([0-9]{1,9})\.([0-9]{1,9})\.([0-9]{1,9})"
RELEASE_TAG_PATTERN = re.compile(rf"refs/tags/v{VERSION_NUMBER}")
VERSION_PATTERN = re.compile(rf"v?{VERSION_NUMBER}")
UPDATE_CHECK_TIMEOUT_SECONDS = 15
UPDATE_INSTALL_TIMEOUT_SECONDS = 45 * 60
STALE_UPDATE_SECONDS = 60 * 60

UPDATE_ERRORS = (OSError, KeyError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired)

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ReleaseCheck:
    current_version: str
    latest_version: str = ""
    update_available: bool = False
    error: str = ""


@dataclass(frozen=True)
class SoftwareUpdateState:
    phase: str = "idle"
    target_version: str = ""
    message: str = ""
    updated_at: float = 0.0
    display_ready: bool = False


def run_command(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, check=True, text=True, **kwargs)


def version_tuple(value: str) -> tuple[int, int, int] | None:
    match = VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def latest_release_tag(output: str) -> str:
    versions: list[tuple[tuple[int, int, int], str]] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        match = RELEASE_TAG_PATTERN.fullmatch(fields[1])
        if match is None:
            continue
        version = tuple(int(part) for part in match.groups())
        versions.append((version, f"v{'.'.join(match.groups())}"))
    return max(versions)[1] if versions else ""


def check_for_update(
    current_version: str = __version__,
    repo_url: str | None = None,
    runner: CommandRunner = run_command,
) -> ReleaseCheck:
    repo_url = repo_url or os.environ.get("CHAPTER_REPO_URL", DEFAULT_REPO_URL)
    current = version_tuple(current_version)
    if current is None:
        return ReleaseCheck(current_version=current_version, error="Invalid installed version.")
    try:
        result = runner(
            ["git", "ls-remote", "--refs", "--tags", "--", repo_url, "v*"],
            timeout=UPDATE_CHECK_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ReleaseCheck(
            current_version=current_version,
            error="Unable to reach the Chapter release repository.",
        )
    latest_tag = latest_release_tag(result.stdout)
    latest = version_tuple(latest_tag)
    if latest is None:
        return ReleaseCheck(
            current_version=current_version,
            error="No stable Chapter releases were found.",
        )
    return ReleaseCheck(
        current_version=current_version,
        latest_version=latest_tag.removeprefix("v"),
        update_available=latest > current,
    )


def update_status_payload() -> dict:
    state = load_update_state()
    update_is_active = update_state_is_active(state)
    if update_is_active:
        return {
            "currentVersion": __version__,
            "latestVersion": state.target_version,
            "updateAvailable": False,
            "phase": "installing",
            "installationStage": state.phase,
            "message": state.message or "Installing the software update.",
        }

    release = check_for_update()
    message = ""
    phase = "available" if release.update_available else "current"
    if state.phase == "failed":
        phase = "failed"
        message = state.message or "The previous software update failed."
    elif state.phase in ("installing", "verifying", "rolling_back"):
        phase = "failed"
        message = "The previous update did not finish. You can try installing it again."
    elif state.phase == "completed" and state.target_version == release.current_version and not release.update_available:
        phase = "completed"
        message = state.message or f"Updated successfully to v{release.current_version}."
    elif release.error:
        phase = "error"
        message = release.error
    return {
        "currentVersion": release.current_version,
        "latestVersion": release.latest_version,
        "updateAvailable": release.update_available,
        "phase": phase,
        "message": message,
    }


def update_state_is_active(
    state: SoftwareUpdateState,
    now: float | None = None,
) -> bool:
    current_time = time.time() if now is None else now
    return (
        state.phase in ("installing", "verifying", "rolling_back")
        and state.updated_at > 0
        and current_time - state.updated_at < STALE_UPDATE_SECONDS
    )


def update_state_path() -> Path:
    configured_dir = os.environ.get("ABS_KIDS_PLAYER_STATE_DIR", "").strip()
    if configured_dir:
        return Path(configured_dir).expanduser() / UPDATE_STATE_FILENAME
    return Path.home() / ".local" / "state" / "abs-kids-player" / UPDATE_STATE_FILENAME


def load_update_state() -> SoftwareUpdateState:
    path = update_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return SoftwareUpdateState(
                phase=str(data.get("phase", "idle")),
                target_version=str(data.get("target_version", "")),
                message=str(data.get("message", "")),
                updated_at=float(data.get("updated_at") or 0),
                display_ready=bool(data.get("display_ready")),
            )
    except (OSError, TypeError, ValueError):
        pass
    return SoftwareUpdateState()


def save_update_state(state: SoftwareUpdateState) -> None:
    write_json_atomically(update_state_path(), asdict(state), durable=True)


def mark_update_display_ready() -> bool:
    state = load_update_state()
    if state.phase != "installing":
        return False
    if state.display_ready:
        return True
    save_update_state(
        SoftwareUpdateState(
            phase=state.phase,
            target_version=state.target_version,
            message=state.message,
            updated_at=state.updated_at,
            display_ready=True,
        )
    )
    return True


def wait_for_update_display_ready(
    timeout_seconds: float = 12.0,
    poll_seconds: float = 0.1,
    state_loader: Callable[[], SoftwareUpdateState] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    load_state = state_loader or load_update_state
    deadline = clock() + max(0.0, timeout_seconds)
    while True:
        state = load_state()
        if state.phase != "installing":
            return False
        if state.display_ready:
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleeper(min(max(poll_seconds, 0.01), remaining))


def concise_error(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        detail = (error.stderr or error.stdout or "").strip().splitlines()
        if detail:
            return detail[-1][:240]
    return str(error)[:540] or "Unknown update error."


def install_latest_release(
    runner: CommandRunner = run_command,
    repo_url: str | None = None,
    install_user: str | None = None,
    install_dir: str | None = None,
    *,
    health_check: Callable[[str, float, CommandRunner], None] = wait_for_player_services,
) -> str:
    repo_url = repo_url or os.environ.get("CHAPTER_REPO_URL", DEFAULT_REPO_URL)
    install_user = install_user or os.environ.get("CHAPTER_INSTALL_USER", DEFAULT_INSTALL_USER)
    install_dir = install_dir or os.environ.get("CHAPTER_INSTALL_DIR", DEFAULT_INSTALL_DIR)
    requested = load_update_state()
    expected_version = ""
    if requested.phase == "installing" and update_state_is_active(requested):
        expected_version = requested.target_version
    release = check_for_update(repo_url=repo_url, runner=runner)
    error = release.error
    if not error and expected_version and (not release.update_available or release.latest_version != expected_version):
        error = "The available release changed. Review the current version and try again."
    if error:
        save_update_state(
            SoftwareUpdateState(
                phase="failed",
                target_version=expected_version,
                message=f"Update failed: {error}",
                updated_at=time.time(),
            )
        )
        raise RuntimeError(error)
    if not release.update_available:
        save_update_state(
            SoftwareUpdateState(
                phase="completed",
                target_version=release.current_version,
                message=f"v{release.current_version} is already up to date.",
                updated_at=time.time(),
            )
        )
        return release.current_version

    target_tag = f"v{release.latest_version}"

    def state(phase: str, message: str) -> None:
        save_update_state(SoftwareUpdateState(
            phase=phase, target_version=release.latest_version, message=message, updated_at=time.time(),
        ))

    def git(*args: str, timeout: int = 30) -> str:
        return runner(
            ["git", "-C", install_dir, *args],
            timeout=timeout,
        ).stdout

    def restart_and_check(version: str) -> None:
        restarted_at = time.time()
        for unit in (SETUP_SERVICE, OLED_SERVICE):
            runner(["systemctl", "restart", unit], timeout=30)
        health_check(version, restarted_at, runner)

    state("installing", f"Installing {target_tag}. The player will restart when it is ready.")
    try:
        require_managed_checkout(Path(install_dir))
        if git("status", "--porcelain").strip():
            raise RuntimeError("The installed checkout has local changes. Keep a clean release checkout for online updates.")
        previous_commit = git("rev-parse", "--verify", "HEAD").strip()
        git("fetch", "--no-tags", "--", repo_url, f"refs/tags/{target_tag}", timeout=120)
        target_commit = git("rev-parse", "--verify", "FETCH_HEAD^{commit}").strip()
        installer = git("show", f"{target_commit}:scripts/install-raspberry-pi.sh")
        if not installer.startswith("#!/usr/bin/env bash"):
            raise RuntimeError("The release installer could not be verified.")
        if "# Chapter install layout: root-owned-v1" not in installer.splitlines()[:4]:
            raise RuntimeError("This release installer does not support a root-owned installation.")
        git("update-ref", PREVIOUS_RELEASE_REF, previous_commit)

        root = recovery_directory()
        with recovery_lock(root) as acquired:
            if not acquired:
                raise RuntimeError("Another Chapter update or recovery is running.")
            with tempfile.TemporaryDirectory(prefix="chapter-update-") as temporary_dir:
                installer_path = Path(temporary_dir) / "install-raspberry-pi.sh"
                installer_path.write_text(installer, encoding="utf-8")
                installer_path.chmod(0o700)
                journal = retain_deployment(
                    root, installer_files(), install_dir=install_dir, install_user=install_user,
                    checkout_owner="root", previous_commit=previous_commit, previous_version=release.current_version,
                    target_version=release.latest_version, state_file=str(update_state_path()),
                )
                try:
                    runner(
                        [
                            "bash", str(installer_path), "--release-ref", target_commit, "--repo-url", repo_url,
                            "--user", install_user, "--install-dir", install_dir, "--no-reboot",
                        ],
                        timeout=UPDATE_INSTALL_TIMEOUT_SECONDS,
                    )
                    state("verifying", f"Checking the setup and OLED services for {target_tag}.")
                    restart_and_check(release.latest_version)
                except UPDATE_ERRORS as error:
                    detail = concise_error(error)
                    try:
                        state("rolling_back", f"Restoring the previous build after {target_tag} failed.")
                        try:
                            runner(["systemctl", "stop", *PLAYER_SERVICES], timeout=30)
                        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                            pass
                        restore_deployment(journal, runner)
                        restart_and_check(release.current_version)
                        discard_journal(journal)
                    except UPDATE_ERRORS as recovery_error:
                        raise RuntimeError(
                            f"{detail} Automatic rollback could not be verified: {concise_error(recovery_error)}"
                        ) from recovery_error
                    raise RuntimeError(
                        f"{detail} Restored the previous build (v{release.current_version})."
                    ) from error
                state("completed", f"Updated successfully to {target_tag}.")
                discard_journal(journal)
        return release.latest_version
    except UPDATE_ERRORS as error:
        state("failed", f"Update failed: {concise_error(error)}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Check for and install Chapter software updates.")
    parser.add_argument("command", choices=("check", "install"))
    args = parser.parse_args()
    if args.command == "check":
        print(json.dumps(update_status_payload(), indent=2))
        return
    installed_version = install_latest_release()
    print(f"Chapter Player v{installed_version} is installed.", flush=True)


if __name__ == "__main__":
    main()
