"""Exercise persistent service selection with the Pi's overlapping trial overrides."""

import io
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.local_release import release


class LocalReleaseTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.bundle = self.root / "opt/chapter-player/package"
        self.fallback = self.bundle / "fallback"
        self.candidate = self.bundle / "candidate"
        for path in (self.fallback, self.candidate):
            path.mkdir(parents=True)
        self.systemd = self.root / "etc/systemd/system"
        self.ram = self.root / "run/systemd/system"
        self.records = self.root / "var/lib/chapter-local-release"
        self.records.mkdir(parents=True)
        self.boot = self.root / "boot-id"
        self.boot.write_text("boot-one")
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
        self.old = Path("/home/chapter/chapter-bluetooth-123456789abc")
        self.manifest = {
            "fallback_archive": "123456789abc",
            "fallback": "matched",
            "candidate": "0123456789ab",
            "package": "test",
        }
        (self.bundle / "release.json").write_text(json.dumps(self.manifest))
        self.running = dict.fromkeys(release.LIVE, self.old)
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
            for root, name, folder in (
                (self.systemd, "zzzzz-chapter-reboot-test", "/old-reboot"),
                (self.systemd, "zzzzzzz-chapter-plugin-preload", "/old-preload"),
                (self.ram, "zzzzzzzzzz-chapter-bluetooth-test", str(self.old)),
            ):
                path = root / (unit + ".d") / (name + ".conf")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("[Service]\nWorkingDirectory=" + folder + "\n")

    def command(self, *args, **kwargs):
        if args[:2] == ("systemctl", "show"):
            return "enabled"
        if args[:2] == ("systemctl", "start"):
            self.start_services()
        if args[0] == "runuser":
            return "Package on disk at that path=matched"
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
        self.boot.write_text("boot-two")
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

    def test_boot_during_switch_recovers_even_if_only_one_override_was_written(self):
        release.select_release(self.candidate)
        (self.records / "pending.json").write_text(json.dumps({"boot": "boot-one"}))
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
        with self.assertRaisesRegex(RuntimeError, "packaged fallback"):
            release.activate(self.manifest)
        self.assertFalse(release.CURRENT.exists())
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

    def select_previous_package(self):
        previous = self.bundle.parent / "chapter-local-7f690f23e02c9362"
        selected = previous / "candidate"
        for root in (selected, self.fallback):
            (root / "deploy").mkdir(parents=True)
            (root / "deploy/audiobookshelf-player-port80-proxy").write_bytes(b"previous proxy")
        (previous / "release.json").write_text(
            json.dumps({"package": previous.name, "candidate": self.manifest["fallback"]})
        )
        release.select_release(selected)
        self.running.update(dict.fromkeys(release.LIVE, selected))
        self.manifest.pop("fallback_archive")
        self.manifest["previous_package"] = previous.name
        (self.bundle / "release.json").write_text(json.dumps(self.manifest))
        return selected

    def test_upgrade_from_named_previous_candidate_survives_reboot(self):
        selected = self.select_previous_package()
        release.activate(self.manifest)
        self.runner.assert_any_call("dpkg", "--verify", selected.parent.name)
        self.reboot()
        release.verify_boot()

    def test_failed_upgrade_restores_previous_player_and_preserves_books(self):
        self.select_previous_package()
        self.health.side_effect = [RuntimeError("missing navigation dial"), None]
        with self.assertRaisesRegex(RuntimeError, "restored"):
            release.activate(self.manifest)
        self.reboot()
        self.require_running(self.fallback, 0)
        release.confirm_books(json.loads((self.records / "before.json").read_bytes()))

    def test_repair_can_upgrade_the_verified_rollback_slot_of_a_failed_release(self):
        accepted = self.select_previous_package().parent
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
        selected = self.select_previous_package()
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
        self.select_previous_package()
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
