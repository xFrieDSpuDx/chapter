"""Handle Raspberry Pi GPIO for the rotary knobs and speaker amplifier."""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Ky040Pins:
    clk: int
    dt: int
    sw: int
    reversed: bool = False


@dataclass(frozen=True)
class InputEvent:
    name: str
    steps: int = 0
    received_at: float = field(default_factory=time.monotonic, compare=False, repr=False)


class AmpShutdownPin:
    def __init__(self, pin: int) -> None:
        from gpiozero import DigitalInputDevice, OutputDevice

        self.pin = pin
        self._input_device = DigitalInputDevice
        self._output_device = OutputDevice
        self.input = None
        self.output = None
        self.set_shutdown(False)

    def set_shutdown(self, shutdown: bool) -> None:
        if shutdown:
            if self.input is not None:
                self.input.close()
                self.input = None
            if self.output is None:
                self.output = self._output_device(self.pin, initial_value=False)
            else:
                self.output.off()
            return

        if self.output is not None:
            self.output.close()
            self.output = None
        if self.input is None:
            # Keep SD/MODE high-Z with no internal pull so the breakout's mode resistor
            # selects its default mono mix while still letting us pull low for shutdown.
            self.input = self._input_device(self.pin, pull_up=None, active_state=True)

    def close(self) -> None:
        if self.output is not None:
            self.output.close()
            self.output = None
        if self.input is not None:
            self.input.close()
            self.input = None


class RotaryEncoderInputs:
    def __init__(
        self,
        events: queue.SimpleQueue[InputEvent],
        nav: Ky040Pins = Ky040Pins(clk=5, dt=6, sw=13),
        volume: Ky040Pins = Ky040Pins(clk=12, dt=16, sw=26),
        nav_hold_seconds: int = 3,
        volume_hold_seconds: int = 10,
    ) -> None:
        from gpiozero import Button

        self.events = events
        self.nav_hold_fired = False
        self.volume_hold_fired = False
        self.nav_encoder = QuadratureInput(
            nav.clk,
            nav.dt,
            lambda step: events.put(InputEvent("nav", step)),
            reversed=nav.reversed,
        )
        self.volume_encoder = QuadratureInput(
            volume.clk,
            volume.dt,
            lambda step: events.put(InputEvent("volume", step)),
            reversed=volume.reversed,
        )
        self.nav_button = Button(
            nav.sw,
            pull_up=True,
            bounce_time=0.05,
            hold_time=nav_hold_seconds,
            hold_repeat=False,
        )
        self.volume_button = Button(
            volume.sw,
            pull_up=True,
            bounce_time=0.05,
            hold_time=volume_hold_seconds,
            hold_repeat=False,
        )

        self.nav_button.when_held = self.on_nav_held
        self.nav_button.when_released = self.on_nav_released
        self.volume_button.when_held = self.on_volume_held
        self.volume_button.when_released = self.on_volume_released

    def on_nav_held(self) -> None:
        self.nav_hold_fired = True
        self.events.put(InputEvent("nav_hold"))

    def on_nav_released(self) -> None:
        if self.nav_hold_fired:
            self.nav_hold_fired = False
            return
        self.events.put(InputEvent("nav_click"))

    def on_volume_held(self) -> None:
        self.volume_hold_fired = True
        self.events.put(InputEvent("show_ip"))

    def on_volume_released(self) -> None:
        if self.volume_hold_fired:
            self.volume_hold_fired = False
            return
        self.events.put(InputEvent("volume_click"))


class NavigationRotaryInput:
    def __init__(
        self,
        events: queue.SimpleQueue[InputEvent],
        nav: Ky040Pins = Ky040Pins(clk=5, dt=6, sw=13),
        nav_hold_seconds: int = 3,
    ) -> None:
        from gpiozero import Button

        self.events = events
        self.nav_hold_fired = False
        self.encoder = QuadratureInput(
            nav.clk,
            nav.dt,
            lambda step: events.put(InputEvent("nav", step)),
            reversed=nav.reversed,
        )
        self.button = Button(
            nav.sw,
            pull_up=True,
            bounce_time=0.05,
            hold_time=nav_hold_seconds,
            hold_repeat=False,
        )
        self.button.when_held = self.on_held
        self.button.when_released = self.on_released

    def on_held(self) -> None:
        self.nav_hold_fired = True
        self.events.put(InputEvent("nav_hold"))

    def on_released(self) -> None:
        if self.nav_hold_fired:
            self.nav_hold_fired = False
            return
        self.events.put(InputEvent("nav_click"))


class VolumeRotaryInput:
    def __init__(
        self,
        events: queue.SimpleQueue[InputEvent],
        volume: Ky040Pins = Ky040Pins(clk=12, dt=16, sw=26),
        volume_hold_seconds: int = 10,
    ) -> None:
        from gpiozero import Button

        self.events = events
        self.volume_hold_fired = False
        self.encoder = QuadratureInput(
            volume.clk,
            volume.dt,
            lambda step: events.put(InputEvent("volume", step)),
            reversed=volume.reversed,
        )
        self.button = Button(
            volume.sw,
            pull_up=True,
            bounce_time=0.05,
            hold_time=volume_hold_seconds,
            hold_repeat=False,
        )

        self.button.when_held = self.on_held
        self.button.when_released = self.on_released

    def on_held(self) -> None:
        self.volume_hold_fired = True
        self.events.put(InputEvent("show_ip"))

    def on_released(self) -> None:
        if self.volume_hold_fired:
            self.volume_hold_fired = False
            return
        self.events.put(InputEvent("volume_click"))


class QuadratureInput:
    transitions = {
        (0b00, 0b01): 1,
        (0b01, 0b11): 1,
        (0b11, 0b10): 1,
        (0b10, 0b00): 1,
        (0b00, 0b10): -1,
        (0b10, 0b11): -1,
        (0b11, 0b01): -1,
        (0b01, 0b00): -1,
    }

    def __init__(
        self,
        clk_pin: int,
        dt_pin: int,
        on_step,
        steps_per_detent: int = 4,
        reversed: bool = False,
    ) -> None:
        from gpiozero import DigitalInputDevice

        self.clk = DigitalInputDevice(clk_pin, pull_up=True, bounce_time=0.001)
        self.dt = DigitalInputDevice(dt_pin, pull_up=True, bounce_time=0.001)
        self.on_step = on_step
        self.steps_per_detent = steps_per_detent
        self.reversed = reversed
        self.accumulator = 0
        self.previous = self.state
        self.clk.when_activated = self.changed
        self.clk.when_deactivated = self.changed
        self.dt.when_activated = self.changed
        self.dt.when_deactivated = self.changed

    @property
    def state(self) -> int:
        return (int(self.clk.value) << 1) | int(self.dt.value)

    def changed(self) -> None:
        current = self.state
        delta = self.transitions.get((self.previous, current), 0)
        self.previous = current
        if not delta:
            return
        self.accumulator += delta
        if abs(self.accumulator) >= self.steps_per_detent:
            step = 1 if self.accumulator > 0 else -1
            self.on_step(-step if self.reversed else step)
            self.accumulator = 0
