import threading
import time
import unittest
from concurrent.futures import CancelledError
from queue import SimpleQueue
from unittest.mock import Mock, patch

from abs_kids_player.appliance_io import InputEvent
from abs_kids_player.bluetooth_audio import BluetoothActionResult, CommandResult
from abs_kids_player.bluetooth_connection import (
    connect_speaker,
    disconnect_speaker,
    playback_endpoint_ready,
)
from abs_kids_player.bluetooth_picker import SpeakerPicker
from abs_kids_player.bluetooth_speakers import (
    AUDIO_SINK,
    BluezDiscovery,
    Speaker,
    SpeakerError,
    discover_speakers,
    speaker_from_properties,
)
from abs_kids_player.oled_input import handle_oled_input_events
from abs_kids_player.rotary_ui import ApplianceMenu, Screen


NEW = Speaker("AA:BB:CC:DD:EE:01", "JBL Go")
SAVED = Speaker("AA:BB:CC:DD:EE:02", "Bedroom", paired=True)
OK = CommandResult(0, "")


class DiscoveryTest(unittest.TestCase):
    def test_remembered_and_live_results_arrive_before_scan_ends(self):
        stop = threading.Event()
        backend = Mock()
        backend.speakers.side_effect = [(SAVED,), (SAVED, NEW)]
        received = []
        def publish(speakers):
            received.append((speakers, backend.start.called, backend.stop.called))
            if NEW in speakers:
                stop.set()
        discover_speakers(publish, stop, backend=lambda: backend,
                          power=lambda enabled: BluetoothActionResult(True, "On"))
        self.assertEqual(received, [((SAVED,), False, False), ((SAVED, NEW), True, False)])
        backend.stop.assert_called_once_with()

    def test_addresses_and_metadata_distinguish_paired_nearby_and_stale_devices(self):
        base = {"Address": NEW.address, "Alias": NEW.name, "UUIDs": [AUDIO_SINK]}
        self.assertIsNone(speaker_from_properties(base))
        self.assertEqual(speaker_from_properties(dict(base, RSSI=-80)), NEW)
        self.assertTrue(speaker_from_properties(dict(base, Bonded=True)).paired)
        self.assertIsNone(speaker_from_properties(dict(base, RSSI=-20, UUIDs=["other"], Icon="input-mouse")))
        self.assertEqual(speaker_from_properties({"Address": NEW.address, "Name": "JBL Go", "RSSI": -60}), NEW)

    def test_new_bluez_object_is_visible_even_without_optional_signal_strength(self):
        db = BluezDiscovery.__new__(BluezDiscovery)
        db.adapter = "/org/bluez/hci0"
        db._cached_paths, db._seen = None, set()
        entry = {"org.bluez.Device1": {"Address": NEW.address, "Name": NEW.name, "UUIDs": [AUDIO_SINK]}}
        db.objects = Mock(side_effect=[{}, {db.adapter + "/new": entry, "/org/bluez/hci1/new": entry}])
        self.assertEqual(db.speakers(), ())
        self.assertEqual(db.speakers(), (NEW,))

    def test_no_speakers_and_scan_error_both_release_owned_discovery(self):
        for result in ((), RuntimeError("adapter disappeared")):
            backend = Mock()
            backend.speakers.side_effect = [(), result]
            with self.subTest(result=result):
                def discover():
                    discover_speakers(Mock(), threading.Event(), seconds=0, backend=lambda: backend,
                                      power=lambda _: BluetoothActionResult(True, "On"))
                if isinstance(result, Exception):
                    with self.assertRaises(RuntimeError):
                        discover()
                else:
                    discover()
                backend.stop.assert_called_once_with()


class ConnectionTest(unittest.TestCase):
    def test_pair_trust_connect_and_wait_for_selected_playback_endpoint(self):
        runner = Mock(side_effect=[CommandResult(0, "Paired: no"), CommandResult(0, "Paired: yes"), OK, OK])
        pair, progress = Mock(return_value=OK), Mock()
        cancel = threading.Event()
        with patch.object(cancel, "wait"):
            connect_speaker(NEW, progress, cancel=cancel, runner=runner, pair=pair, ready=Mock(side_effect=[False, True]))
        self.assertEqual([c.args[0] for c in progress.call_args_list], ["Pairing", "Trust", "Connection", "Audio output"])
        pair.assert_called_once_with(["pair", NEW.address], 25)
        self.assertEqual(runner.call_args_list[-1].args[0], ["connect", NEW.address, AUDIO_SINK])

    def test_reconnect_and_retry_never_pair_a_remembered_speaker_again(self):
        info = CommandResult(0, "Paired: yes\nTrusted: yes\nConnected: no")
        runner = Mock(side_effect=[info, CommandResult(1, "Connection refused"), info, OK])
        pair = Mock()
        with self.assertRaises(SpeakerError) as error:
            connect_speaker(SAVED, Mock(), runner=runner, pair=pair, ready=lambda _: True)
        self.assertEqual(error.exception.stage, "Connection")
        connect_speaker(SAVED, Mock(), runner=runner, pair=pair, ready=lambda _: True)
        pair.assert_not_called()
        self.assertEqual([c.args[0][0] for c in runner.call_args_list], ["info", "connect", "info", "connect"])

    def test_pair_and_trust_failures_never_fall_through_to_connect(self):
        for stage, state in (("Pairing", "Paired: no"), ("Trust", "Paired: yes")):
            with self.subTest(stage=stage):
                fail = CommandResult(1, "Failed: org.bluez.Error.Rejected")
                runner = Mock(side_effect=[CommandResult(0, state), fail])
                with self.assertRaises(SpeakerError) as error:
                    connect_speaker(NEW, Mock(), runner=runner, pair=Mock(return_value=fail), ready=Mock())
                self.assertEqual(error.exception.stage, stage)
                self.assertNotIn("connect", [c.args[0][0] for c in runner.call_args_list])

    def test_connected_link_without_audio_fails_then_retries_only_missing_work(self):
        state = CommandResult(0, "Connected: yes\nTrusted: yes\nBonded: yes")
        runner = Mock(side_effect=[state, OK, state])
        pair = Mock()
        with self.assertRaises(SpeakerError) as error:
            connect_speaker(SAVED, Mock(), runner=runner, pair=pair, ready=lambda _: False, audio_wait=0)
        self.assertEqual(error.exception.stage, "Audio output")
        connect_speaker(SAVED, Mock(), runner=runner, pair=pair, ready=lambda _: True)
        self.assertEqual([c.args[0][0] for c in runner.call_args_list], ["info", "connect", "info"])
        pair.assert_not_called()

    def test_endpoint_requires_the_selected_address_and_playback_profile(self):
        path = "/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_02/"
        with (patch("abs_kids_player.bluetooth_connection.shutil.which", return_value="bluealsa-cli"),
              patch("abs_kids_player.bluetooth_connection.subprocess.run") as run):
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            run.return_value.stdout = path + "a2dpsnk/source\n" + path.replace("02/", "01/") + "a2dpsrc/sink"
            self.assertFalse(playback_endpoint_ready(SAVED.address))
            run.return_value.stdout += "\n" + path + "a2dpsrc/sink"
            self.assertTrue(playback_endpoint_ready(SAVED.address))

    def test_disconnect_keeps_bond(self):
        runner = Mock(return_value=OK)
        disconnect_speaker(SAVED, Mock(), runner=runner)
        runner.assert_called_once_with(["disconnect", SAVED.address], 10)

    def test_leaving_during_pairing_does_not_trust_or_connect_after_pair_returns(self):
        cancel = threading.Event()
        runner = Mock(return_value=CommandResult(0, "Paired: no"))
        def pair(args, timeout):
            cancel.set()
            return OK
        with self.assertRaises(CancelledError):
            connect_speaker(NEW, Mock(), cancel=cancel, pair=pair, runner=runner)
        runner.assert_called_once_with(["info", NEW.address], 4)

    def test_leaving_while_checking_the_endpoint_does_not_start_another_connection(self):
        cancel = threading.Event()
        state = CommandResult(0, "Paired: yes\nTrusted: yes\nConnected: yes")
        runner = Mock(return_value=state)
        def ready(address):
            cancel.set()
            return False
        with self.assertRaises(CancelledError):
            connect_speaker(SAVED, Mock(), cancel=cancel, runner=runner, ready=ready)
        runner.assert_called_once_with(["info", SAVED.address], 4)


class PickerTest(unittest.TestCase):
    def setUp(self):
        self.menu = ApplianceMenu()
        self.finish = threading.Event()
        self.addCleanup(lambda: self.menu.speakers.shutdown())
        self.addCleanup(self.finish.set)

    def input(self, name, steps=0):
        inputs = SimpleQueue()
        inputs.put(InputEvent(name, steps))
        handle_oled_input_events(inputs, self.menu, bluetooth_enabled=True)

    def wait_for(self, condition):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = self.menu.speakers.poll()
            if condition(result):
                return result
            time.sleep(0.002)
        self.fail("Expected Bluetooth event did not arrive")

    def open(self, **operations):
        self.menu.speakers = SpeakerPicker(**operations)
        self.input("nav_hold")
        self.input("nav_click")
        self.assertEqual(self.menu.screen, Screen.BLUETOOTH_DEVICES)

    def test_literal_back_and_hold_escape_from_scanning_even_after_late_results(self):
        entered = threading.Event()
        def scan(publish, cancel):
            entered.set()
            self.finish.wait(1)
            publish((NEW,))
        self.open(discover=scan)
        self.assertTrue(entered.wait(1))
        self.input("nav_click")  # Back while the first search is still running
        self.assertEqual(self.menu.screen, Screen.BLUETOOTH)
        self.input("nav", -2)
        self.input("nav_click")
        self.assertEqual(self.menu.screen, Screen.LIBRARY)
        self.finish.set()
        self.menu.speakers.poll()
        self.assertEqual(self.menu.screen, Screen.LIBRARY)
        self.assertFalse(self.menu.speakers.active)

    def test_hold_exits_to_library_during_connect_and_late_success_is_discarded(self):
        started = threading.Event()
        def connect(speaker, progress, *, cancel):
            started.set()
            self.finish.wait(1)
        self.menu.browser.open([])
        self.menu.screen = Screen.BROWSE
        self.open(discover=lambda publish, _: publish((SAVED,)), connect=connect)
        self.wait_for(lambda _: self.menu.render().top == "Reconnect")
        self.input("nav_click")
        self.assertTrue(started.wait(1))
        self.input("nav_hold")
        self.assertEqual(self.menu.screen, Screen.LIBRARY)
        self.finish.set()
        self.assertIsNone(self.menu.speakers.poll())
        self.assertEqual(self.menu.screen, Screen.LIBRARY)

    def test_a_new_scan_can_start_after_leaving_an_unfinished_pairing(self):
        pairing, abandoned = threading.Event(), threading.Event()
        scans = Mock(side_effect=[(NEW,), (SAVED,)])

        def connect(speaker, progress, *, cancel):
            pairing.set()
            if cancel.wait(2):
                abandoned.set()

        self.open(discover=lambda publish, _: publish(scans()), connect=connect)
        self.wait_for(lambda _: self.menu.speakers.selection == NEW.address)
        self.input("nav_click")
        self.assertTrue(pairing.wait(1))
        self.input("nav_hold")
        self.assertEqual(self.menu.screen, Screen.LIBRARY)
        self.input("nav_hold")
        self.input("nav_click")
        self.wait_for(lambda _: self.menu.speakers.devices == (SAVED,))
        self.assertTrue(abandoned.is_set())
        self.assertTrue(self.menu.speakers.active)
        self.assertFalse(self.menu.speakers.failed)
        self.assertEqual(scans.call_count, 2)

    def test_shutdown_releases_discovery_and_skips_a_queued_connection(self):
        started, release, stopped = threading.Event(), threading.Event(), threading.Event()
        backend = Mock()
        backend.speakers.return_value = (NEW,)
        backend.start.side_effect = started.set
        def stop():
            release.wait(2)
            stopped.set()
        backend.stop.side_effect = stop
        connect = Mock()
        def discover(publish, cancel):
            discover_speakers(publish, cancel, backend=lambda: backend,
                              power=lambda _: BluetoothActionResult(True, "Ready"))
        self.open(discover=discover, connect=connect)
        self.assertTrue(started.wait(1))
        self.wait_for(lambda _: self.menu.speakers.selection == NEW.address)
        self.input("nav_click")
        # Back cancels the queued connection while the radio is still stopping its scan.
        self.input("nav_hold")
        release.set()
        self.menu.speakers.shutdown()
        self.assertTrue(stopped.is_set())
        backend.stop.assert_called_once_with()
        connect.assert_not_called()

    def test_selection_survives_new_arrival_and_retry_targets_same_speaker(self):
        redisplay = threading.Event()
        def scan(publish, cancel):
            publish((SAVED, NEW))
            redisplay.wait(1)
            publish((NEW, SAVED))
        attempts = Mock(side_effect=[SpeakerError("Trust", "Denied"), None])
        self.open(discover=scan, connect=attempts)
        self.wait_for(lambda _: self.menu.render().top == "Reconnect")
        self.input("nav", 1)
        redisplay.set()
        self.wait_for(lambda _: self.menu.speakers.devices[0] == NEW)
        self.assertEqual(self.menu.render().device_name, NEW.name)
        self.input("nav_click")
        self.wait_for(lambda _: self.menu.render().top == "Trust failed")
        self.assertEqual(self.menu.render().device_name, "Retry")
        self.input("nav_click")
        result = self.wait_for(lambda event: event is not None)
        self.assertTrue(result.connected)
        self.assertEqual(result.device_address, NEW.address)
        self.assertEqual([c.args[0] for c in attempts.call_args_list], [NEW, NEW])

    def test_saved_speaker_remains_selectable_when_scan_fails(self):
        def scan(publish, _):
            publish((SAVED,))
            raise SpeakerError("Discovery", "Adapter busy")
        self.open(discover=scan)
        self.wait_for(lambda _: self.menu.render().top == "Discovery failed")
        self.input("nav", 1)
        self.assertEqual(self.menu.render().top, "Reconnect")
        self.assertEqual(self.menu.render().device_name, SAVED.name)

    def test_nested_bluetooth_menu_cannot_become_the_back_destination(self):
        self.open(discover=lambda publish, _: None)
        self.menu.open_bluetooth_menu()
        self.input("nav", -2)
        self.input("nav_click")
        self.assertEqual(self.menu.screen, Screen.LIBRARY)
