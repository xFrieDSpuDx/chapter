from __future__ import annotations

import unittest
from unittest.mock import Mock, call, patch

from abs_kids_player.display_frames import (
    SCREEN_OFF,
    HomeFrame,
    PlayingFrame,
    ScreenSaverFrame,
    TwoLineFrame,
)
from abs_kids_player.oled_display import (
    Sh1122Display,
    oled_safe_text,
    scale_grayscale_value,
    screen_saver_contrast_percent,
    screen_saver_word_specs,
)


class OledDisplayTest(unittest.TestCase):
    def test_every_frame_addresses_row_zero_before_pixel_data_and_power_commands(self):
        # Exercise the real flush path. A mock flush hides an unfinished row command.
        display = object.__new__(Sh1122Display)
        display._last_frame = None
        display.draw = display.image = display.draw_two_line = Mock()
        bus = Mock()
        display.command, display.data = bus.command, bus.data
        pixels = bytes(256 * 64 // 2)
        with patch("abs_kids_player.oled_display.pack_sh1122", return_value=pixels):
            display.init_display(start_off=True)
            display.show(SCREEN_OFF)
            dark_boot = list(bus.method_calls)
            bus.reset_mock()
            frame = TwoLineFrame("Library", "A book")
            display.show(frame)
            display.show(SCREEN_OFF)
            display.show(SCREEN_OFF)
            display.show(frame)
            display.show(frame)
        address_and_data = [call.command([0x00, 0x10, 0xB0, 0x00]), call.data(pixels)]
        self.assertEqual(dark_boot[1:], address_and_data)
        self.assertNotIn(0xAF, dark_boot[0].args[0])
        self.assertEqual(bus.method_calls, [
            *address_and_data, call.command([0xAF]), call.command([0xAE]),
            *address_and_data, call.command([0xAF]),
        ])

    def test_dim_night_brightness_still_writes_a_nonzero_contrast(self):
        display = object.__new__(Sh1122Display)
        display.command = Mock()
        for level in (0, 1, 5, 100):
            display.set_brightness(level)
        self.assertEqual(display.command.call_args_list, [
            call([0x81, 0]), call([0x81, 1]), call([0x81, 1]), call([0x81, 128]),
        ])

    def test_power_off_skips_pixel_writes_and_wake_flushes_before_display_on(self):
        display = object.__new__(Sh1122Display)
        frame = TwoLineFrame("Playing", "Chapter three")
        display._last_frame = frame
        spi = Mock()
        display.command, display.flush = spi.command, spi.flush
        display.draw, display.draw_two_line = Mock(), Mock()
        self.assertFalse(display.show(SCREEN_OFF))
        display.show(SCREEN_OFF)
        spi.command.assert_called_once_with([0xAE])
        spi.flush.assert_not_called()
        self.assertTrue(display.show(frame))
        self.assertEqual(spi.method_calls, [call.command([0xAE]), call.flush(), call.command([0xAF])])
        self.assertFalse(display.show(frame))
        spi.flush.assert_called_once()

    def test_scheduled_boot_initialises_and_clears_display_with_panel_off(self):
        display = object.__new__(Sh1122Display)
        display.command, display.clear = Mock(), Mock()
        display.init_display(start_off=True)
        self.assertNotIn(0xAF, display.command.call_args.args[0])
        display.clear.assert_called_once()

    def test_five_percent_preserves_grayscale_pixels_at_half_panel_contrast(self) -> None:
        display = object.__new__(Sh1122Display)
        display._last_frame = None
        display.draw = Mock()
        display.draw_screen_saver = Mock()
        display.apply_grayscale_brightness = Mock()
        display.flush = Mock()
        display.command = Mock()

        display.show(ScreenSaverFrame(frame_index=0, brightness_percent=5))
        display.apply_grayscale_brightness.assert_called_once_with(10)
        self.assertGreater(scale_grayscale_value(255, 10) >> 4, 0)
        display.set_brightness(screen_saver_contrast_percent(5))
        display.set_brightness(screen_saver_contrast_percent(10))
        self.assertEqual([call.args[0] for call in display.command.call_args_list], [[0x81, 64], [0x81, 128]])

    def test_equal_frames_skip_the_spi_transfer_and_changed_indicators_draw(self) -> None:
        display = object.__new__(Sh1122Display)
        display._last_frame = None
        display.draw = Mock()
        display.draw_two_line = Mock()
        display.flush = Mock()
        display.draw_bluetooth_icon = Mock()

        display.show(TwoLineFrame("Ready", "Choose a book"))
        display.show(TwoLineFrame("Ready", "Choose a book"))
        display.flush.assert_called_once_with()

        display.show(TwoLineFrame("Ready", "Choose a book", bluetooth_connected=True))
        display.draw_bluetooth_icon.assert_called_once_with(x=2)
        self.assertEqual(display.flush.call_count, 2)

    def test_frame_is_redrawn_after_a_failed_spi_transfer_or_clear(self) -> None:
        display = object.__new__(Sh1122Display)
        display._last_frame = None
        display.draw = Mock()
        display.draw_two_line = Mock()
        display.flush = Mock(side_effect=[OSError("SPI unavailable"), None, None, None])
        frame = TwoLineFrame("Ready", "Choose a book")

        with self.assertRaises(OSError):
            display.show(frame)
        display.show(frame)
        display.clear()
        display.show(frame)

        self.assertEqual(display.flush.call_count, 4)
        self.assertEqual(display.draw_two_line.call_count, 3)

    def test_home_jump_letter_uses_its_own_larger_font_without_moving_content(self) -> None:
        display = object.__new__(Sh1122Display)
        display.draw = Mock()
        display.font_home_title = object()
        display.font_home_author = object()
        display.font_section_letter = object()
        display.draw_centered_text = Mock()
        display.draw_scrolling_text = Mock()
        display.draw_bottom_left_text = Mock()
        display.draw_bottom_right_text = Mock()

        with (
            patch("abs_kids_player.oled_display.text_width", return_value=20),
            patch(
                "abs_kids_player.oled_display.fit_text",
                side_effect=lambda _draw, text, _font, _width: text,
            ),
        ):
            display.draw_home(
                HomeFrame("Matilda", "By Roald Dahl", section_letter="M", section_letter_fill=200)
            )

        self.assertEqual(display.draw_centered_text.call_args_list[0].kwargs["y"], 12)
        self.assertEqual(display.draw_centered_text.call_args_list[1].kwargs["y"], 34)
        display.draw_bottom_right_text.assert_called_once_with(
            "M",
            display.font_section_letter,
            fill=200,
            margin=3,
        )

    def test_player_fits_all_five_labels_in_one_row(self) -> None:
        display = object.__new__(Sh1122Display)
        display.draw = Mock()
        display.draw.textbbox.side_effect = lambda _xy, text, font: (0, 0, 42 if text == "Pause" else len(text) * 5, 12)
        display.font_top = display.font_bottom = object()
        display.draw_centered_in_box = Mock()

        actions = ("Home", "Prev", "Pause", "Next", "...")
        display.draw_playing(PlayingFrame("Chapter One", "-5:00", actions, 4))

        labels = display.draw_centered_in_box.call_args_list
        self.assertEqual([call.args[0] for call in labels], list(actions))
        self.assertTrue(all(call.args[1] is display.font_bottom for call in labels))
        boxes = [call.args[2] for call in labels]
        self.assertTrue(all(box[3] - box[1] == 18 for box in boxes))
        self.assertTrue(all(left[2] < right[0] for left, right in zip(boxes, boxes[1:])))
        self.assertEqual(boxes[-1], (227, 40, 251, 58))
        self.assertEqual([call.kwargs["fill"] for call in labels], [255, 255, 255, 255, 0])

    def test_oled_text_uses_ascii_apostrophes(self) -> None:
        self.assertEqual(oled_safe_text("Charlotte\u2019s Web"), "Charlotte's Web")
        self.assertEqual(oled_safe_text("Dragon\ufffds Promise"), "Dragon's Promise")

    def test_oled_text_normalizes_other_typographic_punctuation(self) -> None:
        self.assertEqual(
            oled_safe_text("\u201cHello\u201d\u00a0\u2014 wait\u2026"),
            '"Hello" - wait...',
        )

    def test_screen_saver_word_specs_move_chapter_words(self) -> None:
        first = screen_saver_word_specs(frame_index=10, width=256, height=64)
        second = screen_saver_word_specs(frame_index=11, width=256, height=64)

        self.assertEqual(len(first), 8)
        self.assertEqual(first[0]["font_index"], 0)
        self.assertNotEqual(first[0]["x"], second[0]["x"])
        self.assertGreater(second[0]["x"], first[0]["x"])
        self.assertGreater(second[1]["x"], first[1]["x"])
        self.assertNotEqual(
            first[0]["font_index"],
            screen_saver_word_specs(frame_index=106, width=256, height=64)[0]["font_index"],
        )

    def test_screen_saver_grayscale_is_scaled_to_selected_percent(self) -> None:
        self.assertEqual(scale_grayscale_value(255, 10), 26)
        self.assertEqual(scale_grayscale_value(180, 10), 18)
        self.assertEqual(scale_grayscale_value(0, 10), 0)

    def test_sh1122_brightness_uses_perceptual_contrast_curve(self) -> None:
        display = object.__new__(Sh1122Display)
        commands: list[list[int]] = []
        display.command = commands.append

        display.set_brightness(25)
        display.set_brightness(50)
        display.set_brightness(100)

        self.assertEqual(commands, [[0x81, 0x06], [0x81, 0x1C], [0x81, 0x80]])
