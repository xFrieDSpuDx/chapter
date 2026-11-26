"""Exercise permanent package upgrades, reboot recovery and rollback."""

import io
import json
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.local_release import release
from scripts.local_release.pi_inventory import package_id


class LocalReleaseTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.bundle = self.root / "opt/chapter-player/chapter-local-2222222222222222"
        self.fallback = self.bundle / "fallback"
        self.candidate = self.bundle / "candidate"
        for path in (self.fallback, self.candidate):
            path.mkdir(parents=True)
        self.systemd = self.root / "etc/systemd/system"
        self.ram = self.root / "run/systemd/system"
        self.records = self.root / "var/lib/chapter-local-release"
        self.records.mkdir(parents=True)
        self.boot = self.root / "boot-id"
        self.boot.write_text("1")
        self.enterContext(
            patch.multiple(
                release,
                BUNDLE=self.bundle,
                CURRENT=self.bundle.parent / "current",
                RECORDS=self.records,
                SYSTEMD=self.systemd,
                BOOT_ID=self.boot,
                CONFIG=self.root / "config",
                OWNER=self.root / "owner",
            )
        )
        previous = self.bundle.parent / "chapter-local-1111111111111111"
        self.previous = previous / "candidate"
        proxy = Path("deploy/audiobookshelf-player-port80-proxy")
        for directory in (self.previous, self.fallback):
            (directory / proxy.parent).mkdir(parents=True)
            (directory / proxy).write_bytes(b"previous proxy")
        self.manifest = {
            "previous_package": previous.name,
            "fallback": "ab0123456789",
            "candidate": "0123456789ab",
            "package": self.bundle.name,
        }
        (previous / "release.json").write_text(
            json.dumps({"package": previous.name, "candidate": self.manifest["fallback"]})
        )
        (self.bundle / "release.json").write_text(json.dumps(self.manifest))
        release.select_release(self.previous)
        self.running = dict.fromkeys(release.LIVE, self.previous)
        self.books = {
            "complete_books": 2,
            "complete_track_bytes": 409710161,
            "book_ids_and_track_sizes_sha256": "original",
        }
        self.enterContext(patch.object(release, "inventory", side_effect=lambda: dict(self.books)))
        self.enterContext(
            patch.object(release, "process_directory", side_effect=self.running.__getitem__)
        )
        self.enterContext(patch.object(release, "check_storage_paths"))
        self.runner = self.enterContext(patch.object(release, "run", side_effect=self.command))
        self.health = self.enterContext(
            patch.object(release, "check_health", side_effect=self.require_running)
        )
        for unit in release.LIVE:
            directory = self.systemd / (unit + ".d")
            directory.mkdir(parents=True)
            (directory / release.DROP_IN).write_text(
                f"[Service]\nWorkingDirectory={release.CURRENT}\n"
                "Environment=CHAPTER_RELEASE_SOURCE=local CHAPTER_LOCAL_TRIAL=1\n"
            )

    def command(self, *args, **kwargs):
        if args[:2] == ("systemctl", "show"):
            return "enabled"
        if args[:2] == ("systemctl", "start"):
            self.start_services()
        return ""

    def start_services(self):
        for unit in release.LIVE:
            candidates = [
                p
                for root in (self.systemd, self.ram)
                for p in (root / (unit + ".d")).glob("*.conf")
            ]
            directory = None
            for path in sorted(candidates, key=lambda p: p.name):
                for line in path.read_text().splitlines():
                    if line.startswith("WorkingDirectory="):
                        directory = Path(line.split("=", 1)[1]).resolve()
            self.running[unit] = directory

    def require_running(self, target, since):
        self.assertEqual(set(self.running.values()), {target})

    def reboot(self):
        self.boot.write_text(str(int(self.boot.read_text()) + 1))
        for path in self.ram.rglob("*.conf"):
            path.unlink()
        release.recover()
        self.start_services()

    def test_candidate_survives_reboot_with_matching_downloads_and_protected_code(self):
        release.activate(self.manifest)
        with self.assertRaisesRegex(RuntimeError, "same boot"):
            release.verify_boot()
        self.reboot()
        release.verify_boot()
        for unit in release.LIVE:
            override = self.systemd / (unit + ".d") / release.DROP_IN
            self.assertIn("ExecStart=/usr/bin/python3 -EsBu", override.read_text())
        self.assertTrue((self.systemd / "chapter-local-recovery.service").exists())
        self.assertIn(
            "/usr/bin/false",
            (self.systemd / "audiobookshelf-player-update.service.d" / release.DROP_IN).read_text(),
        )
        self.books["book_ids_and_track_sizes_sha256"] = "different"
        with self.assertRaisesRegex(RuntimeError, "inventory"):
            release.verify_boot()

    def test_failed_health_switches_all_services_back_and_survives_reboot(self):
        def unhealthy(target, since):
            if target == self.candidate:
                raise RuntimeError("OLED has no fresh frame")
            self.require_running(target, since)

        self.health.side_effect = unhealthy
        with self.assertRaisesRegex(RuntimeError, "restored"):
            release.activate(self.manifest)
        self.reboot()
        self.require_running(self.fallback, 0)
        self.assertFalse((self.records / "pending.json").exists())

    def test_manual_rollback_restores_the_previous_build_after_reboot(self):
        release.activate(self.manifest)
        self.reboot()
        release.verify_boot()
        with (
            patch.object(release.os, "geteuid", return_value=0),
            patch.object(sys, "argv", ["release.py", "rollback"]),
        ):
            release.main()
        self.require_running(self.fallback, 0)
        self.reboot()
        self.assertEqual(release.CURRENT.resolve(), self.fallback)
        self.require_running(self.fallback, 0)
        release.confirm_books(json.loads((self.records / "before.json").read_bytes()))

    def test_services_drop_an_inherited_trial_flag_and_identify_as_local_packages(self):
        for unit in (*release.LIVE, release.SPLASH):
            path = self.systemd / (unit + ".d") / "earlier.conf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[Service]\nEnvironment=CHAPTER_LOCAL_TRIAL=1\n")

        release.prepare_services()

        for unit in (*release.LIVE, release.SPLASH):
            with self.subTest(unit=unit):
                lines = (self.systemd / (unit + ".d") / release.DROP_IN).read_text().splitlines()
                self.assertIn("Environment=CHAPTER_RELEASE_SOURCE=local", lines)
                self.assertIn("UnsetEnvironment=CHAPTER_LOCAL_TRIAL", lines)
                self.assertFalse(
                    any(
                        "CHAPTER_LOCAL_TRIAL=" in line
                        for line in lines
                        if line.startswith("Environment=")
                    )
                )

    def test_boot_during_switch_recovers_even_if_only_one_override_was_written(self):
        release.select_release(self.candidate)
        (self.records / "pending.json").write_text(json.dumps({"boot": self.boot.read_text()}))
        one = self.systemd / (release.OLED + ".d") / release.DROP_IN
        one.write_text(f"[Service]\nWorkingDirectory={release.CURRENT}\n")
        release.recover()
        self.assertEqual(release.CURRENT.resolve(), self.candidate)
        self.reboot()
        self.require_running(self.fallback, 0)
        self.assertFalse((self.records / "pending.json").exists())

    def test_failed_fallback_retains_the_boot_recovery_record(self):
        self.health.side_effect = RuntimeError("device unavailable")
        with self.assertRaisesRegex(RuntimeError, "device unavailable"):
            release.activate(self.manifest)
        self.assertTrue((self.records / "pending.json").exists())
        self.reboot()
        self.require_running(self.fallback, 0)

    def test_different_running_build_aborts_before_changing_the_services(self):
        self.running[release.WEB] = Path("/unexpected")
        with self.assertRaisesRegex(RuntimeError, "instead of the selected"):
            release.activate(self.manifest)
        self.assertEqual(release.CURRENT.resolve(), self.previous)
        self.assertFalse((self.records / "pending.json").exists())

    def test_packaging_repair_upgrades_selected_build_and_preserves_original_inventory(self):
        previous = self.bundle.parent / "chapter-local-a15136c0dfb649e4"
        (previous / "candidate").mkdir(parents=True)
        previous_manifest = {**self.manifest, "package": previous.name}
        (previous / "release.json").write_text(json.dumps(previous_manifest))
        release.select_release(previous / "candidate")
        self.running.update(dict.fromkeys(release.LIVE, previous / "candidate"))
        old_receipt = {"boot": "first-install", "downloads": {"complete_books": 1}}
        (self.records / "before.json").write_text(json.dumps(old_receipt))
        release.activate(self.manifest)
        self.assertEqual(json.loads((self.records / "before.json").read_bytes()), old_receipt)
        self.reboot()
        release.verify_boot()

    def test_incompatible_prior_package_is_rejected_before_any_service_change(self):
        previous = self.bundle.parent / "chapter-local-a15136c0dfb649e4"
        (previous / "candidate").mkdir(parents=True)
        mismatched = {**self.manifest, "package": previous.name, "fallback": "other-build"}
        (previous / "release.json").write_text(json.dumps(mismatched))
        release.select_release(previous / "candidate")
        with self.assertRaisesRegex(RuntimeError, "different local release"):
            release.activate(self.manifest)
        self.assertEqual(release.CURRENT.resolve(), previous / "candidate")
        self.assertFalse((self.records / "pending.json").exists())

    def test_upgrade_from_named_previous_candidate_survives_reboot(self):
        release.activate(self.manifest)
        self.runner.assert_any_call("dpkg", "--verify", self.previous.parent.name)
        self.reboot()
        release.verify_boot()

    def test_failed_upgrade_restores_previous_player_and_preserves_books(self):
        self.health.side_effect = [RuntimeError("missing navigation dial"), None]
        with self.assertRaisesRegex(RuntimeError, "restored"):
            release.activate(self.manifest)
        self.reboot()
        self.require_running(self.fallback, 0)
        release.confirm_books(json.loads((self.records / "before.json").read_bytes()))

    def test_repair_can_upgrade_the_verified_rollback_slot_of_a_failed_release(self):
        accepted = self.previous.parent
        failed = self.bundle.parent / "chapter-local-0000000000000001"
        fallback = failed / "fallback"
        proxy = Path("deploy/audiobookshelf-player-port80-proxy")
        (fallback / proxy.parent).mkdir(parents=True)
        (fallback / proxy).write_bytes((self.fallback / proxy).read_bytes())
        installed = {
            "package": failed.name, "candidate": "bad-display",
            "previous_package": accepted.name, "fallback": self.manifest["fallback"],
        }
        (failed / "release.json").write_text(json.dumps(installed))
        self.assertFalse(release.compatible_selection(failed / "candidate", self.manifest))
        release.select_release(fallback)
        self.running.update(dict.fromkeys(release.LIVE, fallback))
        self.assertFalse(release.compatible_selection(fallback, {**self.manifest, "fallback": "different"}))
        with patch.object(release, "run", return_value="modified"):
            self.assertFalse(release.compatible_selection(fallback, self.manifest))
        release.activate(self.manifest)
        self.runner.assert_any_call("dpkg", "--verify", failed.name)
        self.reboot()
        release.verify_boot()

    def test_upgrade_requires_matching_previous_build_role_and_proxy(self):
        selected = self.previous
        self.assertFalse(release.compatible_selection(selected.parent / "fallback", self.manifest))
        for change in (
            {"previous_package": "chapter-local-0000000000000000"},
            {"fallback": "different"},
        ):
            with self.subTest(change=change):
                with self.assertRaisesRegex(RuntimeError, "different local release"):
                    release.activate({**self.manifest, **change})
        (selected / "deploy/audiobookshelf-player-port80-proxy").write_bytes(b"different")
        with self.assertRaisesRegex(RuntimeError, "different local release"):
            release.activate(self.manifest)
        self.assertEqual(release.CURRENT.resolve(), selected)
        self.assertFalse((self.records / "pending.json").exists())

    def test_upgrade_rejects_unmanaged_install(self):
        release.CURRENT.unlink()
        with self.assertRaisesRegex(RuntimeError, "previous permanent package"):
            release.activate(self.manifest)
        self.assertFalse((self.records / "pending.json").exists())

    def test_gpio_services_have_separate_private_runtime_directories(self):
        release.prepare_services()
        for unit, directory in (
            (release.OLED, "chapter-oled-gpio"),
            (release.SPLASH, "chapter-splash-gpio"),
        ):
            lines = (self.systemd / (unit + ".d") / release.DROP_IN).read_text().splitlines()
            self.assertIn(f"WorkingDirectory={release.CURRENT}", lines)
            self.assertIn(f"RuntimeDirectory={directory}", lines)
            self.assertIn("RuntimeDirectoryMode=0700", lines)
            self.assertIn(f"Environment=LG_WD=/run/{directory}", lines)


class LocalPackageBuilderTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.stage = self.root / "stage"
        self.previous = self.root / "chapter-local-1111111111111111"
        self.runtime = self.previous / "candidate"
        for name in (
            "abs_kids_player/oled_service.py",
            "abs_kids_player/setup_server.py",
            "deploy/audiobookshelf-player-port80-proxy",
        ):
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        self.build_id = package_id(self.runtime)
        (self.previous / "release.json").write_text(
            json.dumps(
                {
                    "candidate": self.build_id,
                    "package": self.previous.name,
                }
            )
        )

    def build(self, previous):
        builder = Path(__file__).resolve().parents[1] / "scripts/local_release/build.py"
        return subprocess.run(
            [sys.executable, "-B", str(builder), str(self.stage), str(previous)],
            capture_output=True,
            text=True,
        )

    def test_build_copies_the_previous_package_candidate_for_rollback(self):
        result = self.build(self.previous)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest["previous_package"], self.previous.name)
        self.assertEqual(manifest["fallback"], self.build_id)
        fallback = self.stage / "opt/chapter-player" / manifest["package"] / "fallback"
        for original in self.runtime.rglob("*"):
            if original.is_file():
                self.assertEqual(
                    (fallback / original.relative_to(self.runtime)).read_bytes(),
                    original.read_bytes(),
                )

    def test_build_rejects_a_source_archive(self):
        archive = self.root / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            for path in self.runtime.rglob("*"):
                if path.is_file():
                    stream.add(path, arcname=str(path.relative_to(self.runtime)))
        result = self.build(archive)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("extracted directory of the previous permanent Debian package", result.stderr)
        self.assertFalse((self.stage / "DEBIAN/control").exists())

    def test_build_rejects_a_changed_previous_player(self):
        (self.runtime / "abs_kids_player/oled_service.py").write_text("changed")
        result = self.build(self.previous)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("previous player files differ from its manifest", result.stderr)


class DialHealthTest(unittest.TestCase):
    def setUp(self):
        self.build = "b05f25945634"
        self.header = f"Chapter OLED v0.3.12 build={self.build}"
        self.nav = "Navigation rotary enabled on CLK GPIO5, DT GPIO6, SW GPIO13, reversed=True"
        self.volume = "Volume rotary enabled on CLK GPIO12, DT GPIO16, SW GPIO26"
        self.invocation = "1a" * 16
        properties = {"InvocationID": self.invocation, "MainPID": "654"}
        self.enterContext(
            patch.object(release, "property_of", side_effect=lambda unit, key: properties[key])
        )
        self.journal = self.enterContext(patch.object(release, "run"))
        self.journal.return_value = "\n".join((self.header, self.nav, self.volume))

    def test_checks_both_dials_and_build_in_the_current_service_invocation(self):
        release.check_oled_startup(self.build)
        arguments = self.journal.call_args.args
        self.assertIn("_SYSTEMD_INVOCATION_ID=" + self.invocation, arguments)
        self.assertIn("_PID=654", arguments)
        self.assertIn("-b", arguments)
        with self.assertRaisesRegex(ValueError, "selected OLED build"):
            release.check_oled_startup("previous-build")

    def test_missing_journal_or_either_dial_cannot_pass_health(self):
        for enabled, missing in ((self.nav, "volume"), (self.volume, "navigation")):
            with self.subTest(missing=missing):
                self.journal.return_value = self.header + "\n" + enabled
                with self.assertRaisesRegex(ValueError, missing + " dial"):
                    release.check_oled_startup(self.build)
        self.journal.side_effect = subprocess.CalledProcessError(1, "journalctl")
        with self.assertRaisesRegex(ValueError, "waiting"):
            release.check_oled_startup(self.build)

    def test_failed_dial_rejects_release_even_with_two_fresh_display_responses(self):
        target = Path(self.enterContext(tempfile.TemporaryDirectory())) / "candidate"
        (target.parent / "release.json").write_text(json.dumps({"candidate": self.build}))
        self.enterContext(patch.object(release, "running_source", return_value=target))
        opener = self.enterContext(patch.object(release, "build_opener")).return_value
        opener.open.side_effect = lambda *a, **kw: io.StringIO(
            json.dumps({"displayDrawnAt": time.time()})
        )
        for dial in ("Navigation", "Volume"):
            with self.subTest(dial=dial):
                failure = dial + " rotary could not start: permission denied"
                other = self.volume if dial == "Navigation" else self.nav
                self.journal.return_value = "\n".join((self.header, other, failure))
                with self.assertRaisesRegex(release.RotaryStartupError, failure):
                    release.check_health(target, 0)
        self.assertEqual(opener.open.call_count, 4)

    def test_only_the_oled_gpio_directory_can_replace_its_launch_directory(self):
        source = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        with patch.object(release, "process_directory", return_value=release.OLED_GPIO):
            with patch.object(release, "property_of", return_value=str(source)):
                self.assertEqual(release.running_source(release.OLED), source)
                self.assertEqual(release.running_source(release.WEB), release.OLED_GPIO)
        with patch.object(release, "process_directory", return_value=release.SPLASH_GPIO):
            self.assertEqual(release.running_source(release.OLED), release.SPLASH_GPIO)
