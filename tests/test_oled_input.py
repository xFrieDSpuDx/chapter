from __future__ import annotations

import queue
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from abs_kids_player.appliance_io import InputEvent
from abs_kids_player.audio import AudioError, VolumeState
from abs_kids_player.display_frames import (
    BluetoothMenuFrame,
    ResumeChoiceFrame,
    VolumeFrame,
)
from abs_kids_player.models import Book
from abs_kids_player.oled_input import (
    OledInputQueue,
    consume_screen_saver_wake_input,
    drain_input_events,
    handle_input_events_with_fallback,
    handle_oled_input_events,
    input_activity_seen,
)
from abs_kids_player.rotary_ui import ApplianceMenu, CommandType, MenuCommand
from abs_kids_player.volume_controls import DEFAULT_UNMUTE_VOLUME_PERCENT


class OledInputTest(unittest.TestCase):
    def test_press_or_turn_of_either_dial_is_consumed_before_normal_input(self):
        for name in ("nav_click", "volume_click", "nav", "volume", "nav_hold", "show_ip"):
            with self.subTest(name=name):
                events = OledInputQueue()
                events.put(InputEvent(name, 1, received_at=50))
                if name in ("nav", "volume"):
                    events.put(InputEvent(name, 1, received_at=50.1))
                next_input = InputEvent("nav_click", received_at=50.2)
                events.put(next_input)
                self.assertTrue(events.consume_panel_wake())
                self.assertIs(events.get_nowait(), next_input)
                self.assertFalse(events.consume_panel_wake())

    def test_second_turn_of_the_same_dial_is_a_normal_input_after_waking(self):
        events = OledInputQueue()
        events.put(InputEvent("volume", 1, received_at=50))
        events.put(InputEvent("volume", 1, received_at=50.1))
        next_turn = InputEvent("volume", -1, received_at=51)
        events.put(next_turn)
        self.assertTrue(events.consume_panel_wake())
        self.assertIs(events.get_nowait(), next_turn)
        self.assertEqual(events.last_dial_at, 51)

    def test_queued_input_wakes_the_display_without_consuming_the_event(self) -> None:
        events = OledInputQueue()
        event = InputEvent("nav", steps=1)
        events.put_nowait(event)

        self.assertTrue(events.wait_for_input(0))
        self.assertEqual(events.get_nowait(), event)
        self.assertFalse(events.wait_for_input(0))

    def test_gpio_input_interrupts_an_in_progress_display_wait(self) -> None:
        events = OledInputQueue()
        entered_wait = threading.Event()
        original_wait = events._ready.wait

        def wait(timeout):
            entered_wait.set()
            return original_wait(timeout)

        with (
            patch.object(events._ready, "wait", side_effect=wait),
            ThreadPoolExecutor(max_workers=1) as worker,
        ):
            woke = worker.submit(events.wait_for_input, 1)
            self.assertTrue(entered_wait.wait(1))
            events.put(InputEvent("nav_click"))
            self.assertTrue(woke.result(timeout=1))

    def test_nav_event_moves_selected_book(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("nav", steps=1))
        clicks = []
        spoken = []
        menu = ApplianceMenu(
            [
                Book(
                    id="book-1",
                    title="The Hobbit",
                    author="J.R.R. Tolkien",
                    duration=1,
                    cover_url="",
                ),
                Book(id="book-2", title="Matilda", author="Roald Dahl", duration=1, cover_url=""),
            ]
        )

        _frame, _percent, _muted, changed, commands, pause_playback, resume_playback = (
            handle_oled_input_events(
                events,
                menu,
                control_click_feedback=lambda: clicks.append("click"),
                spoken_navigation_feedback=spoken.append,
            )
        )

        self.assertTrue(changed)
        self.assertEqual(clicks, ["click"])
        self.assertEqual(commands, [])
        self.assertFalse(pause_playback)
        self.assertFalse(resume_playback)
        self.assertEqual(menu.render().top, "Matilda")
        self.assertEqual(spoken[0].text, "Matilda")
        self.assertGreater(spoken[0].delay_seconds, 0)

    def test_nav_click_opens_resume_choice_when_book_has_history(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("nav_click"))
        clicks = []
        spoken = []
        speech_cancels = []
        menu = ApplianceMenu(
            [
                Book(
                    id="book-1",
                    title="The Hobbit",
                    author="J.R.R. Tolkien",
                    duration=1,
                    cover_url="",
                    current_time=30,
                    progress=0.1,
                )
            ]
        )

        _frame, _percent, _muted, changed, commands, pause_playback, resume_playback = (
            handle_oled_input_events(
                events,
                menu,
                control_click_feedback=lambda: clicks.append("click"),
                spoken_navigation_feedback=spoken.append,
                spoken_navigation_cancel=lambda: speech_cancels.append("cancel"),
            )
        )

        self.assertTrue(changed)
        self.assertEqual(clicks, ["click"])
        self.assertEqual(commands, [])
        self.assertFalse(pause_playback)
        self.assertFalse(resume_playback)
        frame = menu.render()
        self.assertIsInstance(frame, ResumeChoiceFrame)
        self.assertEqual(frame.top, "The Hobbit")
        self.assertEqual(frame.options, ("Home", "Continue", "Start over"))
        self.assertEqual(frame.selected_index, 1)
        self.assertEqual(spoken[0].text, "Continue")
        self.assertEqual(spoken[0].delay_seconds, 0)
        self.assertEqual(speech_cancels, ["cancel"])

    def test_nav_click_without_history_opens_home_start_choice(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("nav_click"))
        menu = ApplianceMenu(
            [
                Book(
                    id="book-1",
                    title="The Hobbit",
                    author="J.R.R. Tolkien",
                    duration=1,
                    cover_url="",
                )
            ]
        )

        frame, _percent, _muted, changed, commands, pause_playback, resume_playback = (
            handle_oled_input_events(events, menu)
        )

        self.assertTrue(changed)
        self.assertIsNone(frame)
        self.assertEqual(commands, [])
        self.assertFalse(pause_playback)
        self.assertFalse(resume_playback)
        rendered = menu.render()
        self.assertIsInstance(rendered, ResumeChoiceFrame)
        self.assertEqual(rendered.top, "The Hobbit")
        self.assertEqual(rendered.options, ("Home", "Start"))
        self.assertEqual(rendered.selected_index, 1)

        events.put(InputEvent("nav_click", received_at=123.0))
        _frame, _percent, _muted, _changed, commands, _pause, _resume = handle_oled_input_events(
            events, menu
        )
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].type, CommandType.PLAY_BOOK)
        self.assertEqual(commands[0].book_id, "book-1")
        self.assertEqual(commands[0].selected_at, 123.0)

    def test_nav_hold_opens_bluetooth_menu(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("nav_hold"))
        menu = ApplianceMenu()

        frame, _percent, _muted, changed, commands, pause_playback, resume_playback = (
            handle_oled_input_events(
                events,
                menu,
                bluetooth_enabled=False,
            )
        )

        self.assertIsNone(frame)
        self.assertTrue(changed)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].type, CommandType.PAUSE_PLAYBACK)
        self.assertFalse(pause_playback)
        self.assertFalse(resume_playback)
        rendered = menu.render()
        self.assertIsInstance(rendered, BluetoothMenuFrame)
        self.assertFalse(rendered.enabled)
        self.assertEqual(rendered.options, ("Back", "Enable", "Speakers"))

    def test_volume_click_mute_requests_playback_pause(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume_click"))
        menu = ApplianceMenu()

        with patch(
            "abs_kids_player.volume_controls.toggle_mute",
            return_value=VolumeState(percent=50, muted=True),
        ):
            frame, _percent, muted, _changed, _commands, pause_playback, resume_playback = (
                handle_oled_input_events(events, menu)
            )

        self.assertIsInstance(frame, VolumeFrame)
        self.assertEqual(frame.top, "Muted")
        self.assertTrue(muted)
        self.assertTrue(pause_playback)
        self.assertFalse(resume_playback)

    def test_spoken_navigation_announces_volume_at_ten_percent_intervals(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume", steps=1))
        events.put(InputEvent("volume", steps=1))
        menu = ApplianceMenu()
        spoken = []

        frame, percent, muted, _changed, _commands, pause_playback, resume_playback = (
            handle_oled_input_events(
                events,
                menu,
                fallback_percent=50,
                fallback_muted=False,
                software_volume_only=True,
                spoken_navigation_feedback=spoken.append,
            )
        )

        self.assertIsInstance(frame, VolumeFrame)
        self.assertEqual(percent, 60)
        self.assertFalse(muted)
        self.assertFalse(pause_playback)
        self.assertFalse(resume_playback)
        self.assertEqual([selection.text for selection in spoken], ["Volume 60 percent"])

    def test_spoken_navigation_allows_playback_below_ten_percent(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume", steps=-1))
        menu = ApplianceMenu()
        spoken = []

        frame, percent, muted, _changed, _commands, pause_playback, resume_playback = (
            handle_oled_input_events(
                events,
                menu,
                fallback_percent=10,
                fallback_muted=False,
                software_volume_only=True,
                spoken_navigation_feedback=spoken.append,
            )
        )

        self.assertIsInstance(frame, VolumeFrame)
        self.assertEqual(percent, 9)
        self.assertFalse(muted)
        self.assertFalse(pause_playback)
        self.assertFalse(resume_playback)
        self.assertEqual(spoken, [])

    def test_spoken_navigation_announces_mute_without_blocking_it(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume_click"))
        menu = ApplianceMenu()
        spoken = []

        frame, percent, muted, _changed, _commands, pause_playback, resume_playback = (
            handle_oled_input_events(
                events,
                menu,
                fallback_percent=3,
                software_volume_only=True,
                spoken_navigation_feedback=spoken.append,
            )
        )

        self.assertIsInstance(frame, VolumeFrame)
        self.assertEqual(percent, 3)
        self.assertTrue(muted)
        self.assertTrue(pause_playback)
        self.assertFalse(resume_playback)
        self.assertEqual([selection.text for selection in spoken], ["Muted"])

    def test_volume_zero_requests_playback_pause(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume", steps=-1))
        menu = ApplianceMenu()

        with patch(
            "abs_kids_player.volume_controls.change_volume",
            return_value=VolumeState(percent=0, muted=False),
        ):
            frame, _percent, muted, _changed, _commands, pause_playback, resume_playback = (
                handle_oled_input_events(events, menu)
            )

        self.assertIsInstance(frame, VolumeFrame)
        self.assertEqual(frame.top, "Muted")
        self.assertFalse(muted)
        self.assertTrue(pause_playback)
        self.assertFalse(resume_playback)

    def test_volume_up_from_zero_requests_playback_resume(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume", steps=1))
        menu = ApplianceMenu()

        with patch(
            "abs_kids_player.volume_controls.change_volume", side_effect=AudioError("missing audio")
        ):
            frame, percent, muted, _changed, _commands, pause_playback, resume_playback = (
                handle_oled_input_events(
                    events,
                    menu,
                    fallback_percent=0,
                    fallback_muted=False,
                )
            )

        self.assertIsInstance(frame, VolumeFrame)
        self.assertEqual(percent, 1)
        self.assertFalse(muted)
        self.assertFalse(frame.is_muted)
        self.assertFalse(pause_playback)
        self.assertTrue(resume_playback)

    def test_software_volume_only_skips_system_mixer(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume", steps=-1))
        menu = ApplianceMenu()

        with patch("abs_kids_player.volume_controls.change_volume") as change:
            frame, percent, muted, _changed, _commands, pause_playback, resume_playback = (
                handle_oled_input_events(
                    events,
                    menu,
                    fallback_percent=50,
                    fallback_muted=False,
                    software_volume_only=True,
                )
            )

        change.assert_not_called()
        self.assertIsInstance(frame, VolumeFrame)
        self.assertEqual(percent, 45)
        self.assertFalse(muted)
        self.assertFalse(pause_playback)
        self.assertFalse(resume_playback)

    def test_software_volume_only_zero_requests_pause_without_system_mixer(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume", steps=-1))
        menu = ApplianceMenu()

        with patch("abs_kids_player.volume_controls.change_volume") as change:
            frame, percent, muted, _changed, _commands, pause_playback, resume_playback = (
                handle_oled_input_events(
                    events,
                    menu,
                    fallback_percent=1,
                    fallback_muted=False,
                    software_volume_only=True,
                )
            )

        change.assert_not_called()
        self.assertIsInstance(frame, VolumeFrame)
        self.assertEqual(percent, 0)
        self.assertFalse(muted)
        self.assertTrue(frame.is_muted)
        self.assertTrue(pause_playback)
        self.assertFalse(resume_playback)

    def test_volume_click_from_zero_restores_volume_and_requests_resume(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume_click"))
        menu = ApplianceMenu()

        with patch(
            "abs_kids_player.volume_controls.toggle_mute", side_effect=AudioError("missing audio")
        ):
            frame, percent, muted, _changed, _commands, pause_playback, resume_playback = (
                handle_oled_input_events(
                    events,
                    menu,
                    fallback_percent=0,
                    fallback_muted=False,
                )
            )

        self.assertIsInstance(frame, VolumeFrame)
        self.assertEqual(percent, DEFAULT_UNMUTE_VOLUME_PERCENT)
        self.assertFalse(muted)
        self.assertFalse(frame.is_muted)
        self.assertFalse(pause_playback)
        self.assertTrue(resume_playback)

    def test_volume_click_unmute_requests_playback_resume(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume_click"))
        menu = ApplianceMenu()

        with patch(
            "abs_kids_player.volume_controls.toggle_mute",
            return_value=VolumeState(percent=50, muted=False),
        ):
            frame, percent, muted, _changed, _commands, pause_playback, resume_playback = (
                handle_oled_input_events(
                    events,
                    menu,
                    fallback_percent=50,
                    fallback_muted=True,
                )
            )

        self.assertIsInstance(frame, VolumeFrame)
        self.assertEqual(percent, 50)
        self.assertFalse(muted)
        self.assertFalse(frame.is_muted)
        self.assertFalse(pause_playback)
        self.assertTrue(resume_playback)

    def test_mixer_unmute_fallback_restores_previous_low_volume_with_speech(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume_click"))
        spoken = []

        with patch(
            "abs_kids_player.volume_controls.toggle_mute", side_effect=AudioError("missing audio")
        ):
            frame, percent, muted, _changed, _commands, pause_playback, resume_playback = (
                handle_oled_input_events(
                    events,
                    ApplianceMenu(),
                    fallback_percent=5,
                    fallback_muted=True,
                    spoken_navigation_feedback=spoken.append,
                )
            )

        self.assertEqual(frame, VolumeFrame(5))
        self.assertEqual(percent, 5)
        self.assertFalse(muted)
        self.assertFalse(pause_playback)
        self.assertTrue(resume_playback)
        self.assertEqual([selection.text for selection in spoken], ["Volume 5 percent"])

    def test_volume_event_falls_back_to_software_volume_when_audio_control_fails(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume", steps=1))

        with patch(
            "abs_kids_player.volume_controls.change_volume", side_effect=AudioError("missing audio")
        ):
            frame, percent, muted = handle_input_events_with_fallback(
                events,
                fallback_percent=50,
                fallback_muted=False,
            )

        self.assertIsInstance(frame, VolumeFrame)
        self.assertEqual(frame.percent, 55)
        self.assertEqual(percent, 55)
        self.assertFalse(muted)

    def test_volume_click_fallback_shows_muted_without_zeroing_percent(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume_click"))

        with patch(
            "abs_kids_player.volume_controls.toggle_mute", side_effect=AudioError("missing audio")
        ):
            frame, percent, muted = handle_input_events_with_fallback(
                events,
                fallback_percent=50,
                fallback_muted=False,
            )

        self.assertIsInstance(frame, VolumeFrame)
        self.assertEqual(frame.percent, 50)
        self.assertEqual(frame.top, "Muted")
        self.assertTrue(frame.is_muted)
        self.assertEqual(percent, 50)
        self.assertTrue(muted)

    def test_update_mode_discards_rotary_events(self) -> None:
        events: queue.SimpleQueue[InputEvent] = queue.SimpleQueue()
        events.put(InputEvent("nav", 1))
        events.put(InputEvent("volume_click"))

        drain_input_events(events)

        self.assertTrue(events.empty())

    def test_input_activity_seen_wakes_screen_saver(self) -> None:
        self.assertFalse(input_activity_seen(None, False, [], False))
        self.assertTrue(input_activity_seen(VolumeFrame(50), False, [], False))
        self.assertTrue(input_activity_seen(None, True, [], False))
        self.assertTrue(input_activity_seen(None, False, [MenuCommand(CommandType.HOME)], False))
        self.assertTrue(input_activity_seen(None, False, [], True))

    def test_screen_saver_wake_consumes_all_pending_input(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("nav_click"))
        events.put(InputEvent("nav", steps=1))

        woke, quiet_until = consume_screen_saver_wake_input(
            events,
            screen_saver_visible=True,
            now=100.0,
            quiet_until=0.0,
        )

        self.assertTrue(woke)
        self.assertAlmostEqual(quiet_until, 100.35)
        self.assertTrue(events.empty())

    def test_screen_saver_wake_quiet_period_absorbs_residual_events(self) -> None:
        events = queue.SimpleQueue()
        events.put(InputEvent("volume", steps=1))

        woke, quiet_until = consume_screen_saver_wake_input(
            events,
            screen_saver_visible=False,
            now=100.2,
            quiet_until=100.35,
        )

        self.assertFalse(woke)
        self.assertAlmostEqual(quiet_until, 100.55)
        self.assertTrue(events.empty())

    def test_active_screen_does_not_consume_input(self) -> None:
        events = queue.SimpleQueue()
        event = InputEvent("nav_click")
        events.put(event)

        woke, quiet_until = consume_screen_saver_wake_input(
            events,
            screen_saver_visible=False,
            now=101.0,
            quiet_until=100.35,
        )

        self.assertFalse(woke)
        self.assertEqual(quiet_until, 100.35)
        self.assertEqual(events.get_nowait(), event)
