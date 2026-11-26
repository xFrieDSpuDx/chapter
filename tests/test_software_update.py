from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from abs_kids_player import __version__
from abs_kids_player.software_update import (
    ReleaseCheck,
    SoftwareUpdateState,
    check_for_update,
    install_latest_release,
    latest_release_tag,
    load_update_state,
    mark_update_display_ready,
    save_update_state,
    update_state_is_active,
    update_status_payload,
    version_tuple,
    wait_for_update_display_ready,
)
from abs_kids_player.update_recovery import (
    OLED_SERVICE,
    PLAYER_SERVICES,
    PREVIOUS_RELEASE_REF,
    SETUP_SERVICE,
)


RELEASES = """\
1111111111111111111111111111111111111111\trefs/tags/v0.2.2
2222222222222222222222222222222222222222\trefs/tags/v0.3.0
3333333333333333333333333333333333333333\trefs/tags/v0.3.0-rc1
4444444444444444444444444444444444444444\trefs/tags/not-a-release
"""
FUTURE_RELEASES = RELEASES + (
    "5555555555555555555555555555555555555555\trefs/tags/v0.4.0\n"
)


class SoftwareUpdateTest(unittest.TestCase):
    def test_version_parser_only_accepts_stable_semantic_versions(self) -> None:
        self.assertEqual(version_tuple("v1.2.3"), (1, 2, 3))
        self.assertEqual(version_tuple("1.2.3"), (1, 2, 3))
        self.assertIsNone(version_tuple("1.2"))
        self.assertIsNone(version_tuple("v1.2.3-rc1"))

    def test_latest_release_tag_ignores_non_stable_tags(self) -> None:
        self.assertEqual(latest_release_tag(RELEASES), "v0.3.0")

    def test_check_for_update_reports_newer_release(self) -> None:
        commands = []

        def runner(args, **_kwargs):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, stdout=RELEASES, stderr="")

        result = check_for_update(
            current_version="0.2.2",
            repo_url="https://example.test/chapter.git",
            runner=runner,
        )

        self.assertTrue(result.update_available)
        self.assertEqual(result.latest_version, "0.3.0")
        self.assertEqual(
            commands,
            [["git", "ls-remote", "--refs", "--tags", "--", "https://example.test/chapter.git", "v*"]],
        )

    def test_check_for_update_returns_a_safe_network_error(self) -> None:
        def runner(_args, **_kwargs):
            raise subprocess.TimeoutExpired("git", 15)

        result = check_for_update(current_version="0.2.2", runner=runner)

        self.assertFalse(result.update_available)
        self.assertEqual(result.error, "Unable to reach the Chapter release repository.")

    def test_update_checker_uses_the_repository_configured_by_the_installer(self) -> None:
        repo_url = "https://example.test/fork.git"
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=FUTURE_RELEASES))
        with patch.dict(os.environ, {"CHAPTER_REPO_URL": repo_url}):
            result = check_for_update(runner=runner)

        self.assertTrue(result.update_available)
        self.assertIn(repo_url, runner.call_args.args[0])

    def test_installer_rejects_a_newer_version_the_user_did_not_confirm(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ABS_KIDS_PLAYER_STATE_DIR": directory, "CHAPTER_REPO_URL": "https://example.test/fork.git"}),
        ):
            save_update_state(SoftwareUpdateState(phase="installing", target_version="0.3.13", updated_at=time.time()))
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=FUTURE_RELEASES))
            with self.assertRaisesRegex(RuntimeError, "available release changed"):
                install_latest_release(runner=runner)
            self.assertEqual(load_update_state().phase, "failed")

        runner.assert_called_once()
        self.assertIn("https://example.test/fork.git", runner.call_args.args[0])

    def test_status_reports_rollback_as_an_active_stage_without_network_access(self) -> None:
        state = SoftwareUpdateState(phase="rolling_back", target_version="0.4.0", updated_at=time.time())
        with (
            patch("abs_kids_player.software_update.load_update_state", return_value=state),
            patch("abs_kids_player.software_update.check_for_update") as check,
        ):
            payload = update_status_payload()

        check.assert_not_called()
        self.assertEqual(payload["phase"], "installing")
        self.assertEqual(payload["installationStage"], "rolling_back")

    def test_update_state_round_trip_uses_configured_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            state = SoftwareUpdateState(
                phase="installing",
                target_version="0.3.0",
                message="Installing v0.3.0.",
                updated_at=123.0,
                display_ready=True,
            )
            with patch.dict(os.environ, {"ABS_KIDS_PLAYER_STATE_DIR": temporary_dir}):
                save_update_state(state)
                loaded = load_update_state()

            self.assertEqual(loaded, state)
            self.assertEqual((Path(temporary_dir) / "software-update.json").stat().st_mode & 0o777, 0o600)

    def test_oled_can_acknowledge_that_update_screen_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            with patch.dict(os.environ, {"ABS_KIDS_PLAYER_STATE_DIR": temporary_dir}):
                save_update_state(
                    SoftwareUpdateState(
                        phase="installing",
                        message="Preparing the software update.",
                        updated_at=123.0,
                    )
                )

                self.assertTrue(mark_update_display_ready())
                state = load_update_state()

        self.assertTrue(state.display_ready)
        self.assertEqual(state.phase, "installing")
        self.assertEqual(state.updated_at, 123.0)

    def test_updater_waits_until_oled_acknowledges_the_screen(self) -> None:
        states = iter(
            [
                SoftwareUpdateState(phase="installing", updated_at=123.0),
                SoftwareUpdateState(phase="installing", updated_at=123.0, display_ready=True),
            ]
        )
        sleeps: list[float] = []

        ready = wait_for_update_display_ready(
            timeout_seconds=1.0,
            state_loader=lambda: next(states),
            sleeper=sleeps.append,
            clock=lambda: 0.0,
        )

        self.assertTrue(ready)
        self.assertEqual(sleeps, [0.1])

    def test_only_recent_installing_state_is_active(self) -> None:
        active = SoftwareUpdateState(phase="installing", updated_at=100.0)
        stale = SoftwareUpdateState(phase="installing", updated_at=1.0)

        self.assertTrue(update_state_is_active(active, now=101.0))
        self.assertFalse(update_state_is_active(stale, now=10_000.0))
        self.assertFalse(
            update_state_is_active(SoftwareUpdateState(phase="completed", updated_at=100.0), now=101.0)
        )

    def test_status_payload_preserves_failed_update_message(self) -> None:
        with (
            patch(
                "abs_kids_player.software_update.load_update_state",
                return_value=SoftwareUpdateState(
                    phase="failed",
                    target_version="0.3.0",
                    message="Update failed: package error",
                ),
            ),
            patch(
                "abs_kids_player.software_update.check_for_update",
                return_value=ReleaseCheck(
                    current_version="0.2.2",
                    latest_version="0.3.0",
                    update_available=True,
                ),
            ),
        ):
            payload = update_status_payload()

        self.assertEqual(payload["phase"], "failed")
        self.assertEqual(payload["message"], "Update failed: package error")
        self.assertTrue(payload["updateAvailable"])

    def test_status_payload_recovers_from_stale_installing_state(self) -> None:
        with (
            patch(
                "abs_kids_player.software_update.load_update_state",
                return_value=SoftwareUpdateState(
                    phase="installing",
                    target_version="0.3.0",
                    message="Installing v0.3.0.",
                    updated_at=1.0,
                ),
            ),
            patch("abs_kids_player.software_update.time.time", return_value=10_000.0),
            patch(
                "abs_kids_player.software_update.check_for_update",
                return_value=ReleaseCheck(
                    current_version="0.2.2",
                    latest_version="0.3.0",
                    update_available=True,
                ),
            ),
        ):
            payload = update_status_payload()

        self.assertEqual(payload["phase"], "failed")
        self.assertTrue(payload["updateAvailable"])
        self.assertIn("did not finish", payload["message"])

    def fake_install(self, directory: Path, *, fail_at: str = "", recovery_fails: bool = False):
        previous_commit, new_commit = "a" * 40, "b" * 40
        deployment = directory / "oled.service"
        new_file = directory / "new-setup.service"
        deployment.write_text("old service")
        app_data = {"settings.json": "settings", "download.m4b": "audio", "progress.json": "position"}
        for name, value in app_data.items():
            (directory / name).write_text(value)

        calls = []
        current = previous_commit

        def runner(args, **kwargs):
            nonlocal current
            calls.append(args)
            command = args[3] if args[:2] == ["git", "-C"] else ""
            stdout = ""
            if args[:2] == ["git", "ls-remote"]:
                stdout = FUTURE_RELEASES
            elif command == "status" and fail_at == "dirty":
                stdout = " M abs_kids_player/setup_server.py"
            elif command == "rev-parse":
                stdout = previous_commit if args[-1] == "HEAD" else new_commit
            elif command == "show":
                stdout = "#!/usr/bin/env bash\n# Chapter install layout: root-owned-v1\nexit 0\n"
                if fail_at == "old_layout":
                    stdout = "#!/usr/bin/env bash\nexit 0\n"
            elif args[0] == "bash":
                current = new_commit
                deployment.write_text("new service")
                new_file.write_text("new service")
                if fail_at == "installer":
                    raise subprocess.CalledProcessError(1, args, stderr="installer failed")
            elif args[:3] == ["systemctl", "restart", OLED_SERVICE] and fail_at == "restart" and current == new_commit:
                raise subprocess.CalledProcessError(1, args, stderr="restart failed")
            elif command == "checkout":
                current = args[-1]
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        def check_health(version, restarted_at, runner):
            self.assertGreater(restarted_at, 0)
            if version == "0.4.0" and fail_at in ("setup", "oled"):
                raise RuntimeError(f"{fail_at} health failed")
            if version == __version__ and recovery_fails:
                raise RuntimeError("previous build failed its health check")

        health = Mock(side_effect=check_health)
        with (
            patch.dict(os.environ, {
                "ABS_KIDS_PLAYER_STATE_DIR": str(directory),
                "CHAPTER_UPDATE_RECOVERY_DIR": str(directory / "update"),
            }),
            patch("abs_kids_player.update_recovery.os.sync"),
            patch("abs_kids_player.software_update.require_managed_checkout"),
            patch("abs_kids_player.update_recovery.require_managed_checkout"),
            patch("abs_kids_player.software_update.os.geteuid", return_value=1000),
            patch("abs_kids_player.software_update.installer_files", return_value=(deployment, new_file)),
        ):
            if fail_at:
                with self.assertRaises(RuntimeError):
                    install_latest_release(
                        runner=runner, repo_url="https://example.test/chapter.git",
                        install_user="chapter", install_dir="/opt/chapter-player/git", health_check=health,
                    )
            else:
                self.assertEqual(install_latest_release(
                    runner=runner, repo_url="https://example.test/chapter.git",
                    install_user="chapter", install_dir="/opt/chapter-player/git", health_check=health,
                ), "0.4.0")
            state = load_update_state()

        self.assertEqual({name: (directory / name).read_text() for name in app_data}, app_data)
        if fail_at:
            self.assertEqual(deployment.read_text(), "old service")
            self.assertFalse(new_file.exists())
            self.assertEqual(current, previous_commit)
        else:
            self.assertEqual(deployment.read_text(), "new service")
            self.assertEqual(current, new_commit)
        return state, calls, [call.args[0] for call in health.call_args_list]

    def test_older_layout_fails_before_running_a_release_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            state, calls, _ = self.fake_install(Path(directory), fail_at="old_layout")
        self.assertIn("root-owned installation", state.message)
        self.assertFalse(any(args[0] == "bash" for args in calls))

    def test_installer_pins_commits_and_checks_both_services_before_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, calls, versions = self.fake_install(Path(directory))

        self.assertEqual(state.phase, "completed")
        self.assertEqual(versions, ["0.4.0"])
        git_args = [args[3:] for args in calls if args[:2] == ["git", "-C"]]
        self.assertIn(["fetch", "--no-tags", "--", "https://example.test/chapter.git", "refs/tags/v0.4.0"], git_args)
        self.assertIn(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], git_args)
        self.assertIn(["update-ref", PREVIOUS_RELEASE_REF, "a" * 40], git_args)
        self.assertIn(["show", f'{"b" * 40}:scripts/install-raspberry-pi.sh'], git_args)
        install = next(args for args in calls if args[0] == "bash")
        self.assertEqual(install[install.index("--release-ref") + 1], "b" * 40)
        self.assertIn("--no-reboot", install)
        self.assertEqual(calls[-2:], [["systemctl", "restart", SETUP_SERVICE], ["systemctl", "restart", OLED_SERVICE]])

    def test_failed_install_restart_or_health_check_restores_the_previous_build(self) -> None:
        for stage in ("installer", "restart", "setup", "oled"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                state, calls, versions = self.fake_install(Path(directory), fail_at=stage)
                self.assertEqual(state.phase, "failed")
                self.assertIn("Restored the previous build", state.message)
                self.assertEqual(versions, [__version__] if stage in ("installer", "restart") else ["0.4.0", __version__])
                self.assertIn(["systemctl", "stop", *PLAYER_SERVICES], calls)
                self.assertIn(["systemctl", "daemon-reload"], calls)
                self.assertIn(["git", "-C", "/opt/chapter-player/git", "checkout",
                               "--force", "--detach", "a" * 40], calls)

    def test_service_failure_during_rollback_does_not_report_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, _, versions = self.fake_install(Path(directory), fail_at="setup", recovery_fails=True)
        self.assertEqual(versions, ["0.4.0", __version__])
        self.assertIn("rollback could not be verified", state.message)
        self.assertNotIn("Restored the previous build", state.message)

    def test_dirty_checkout_is_rejected_before_stopping_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, calls, versions = self.fake_install(Path(directory), fail_at="dirty")
        self.assertIn("checkout has local changes", state.message)
        self.assertEqual(versions, [])
        self.assertFalse(any(args[0] == "systemctl" for args in calls))


if __name__ == "__main__":
    unittest.main()
