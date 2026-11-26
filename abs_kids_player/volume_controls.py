"""Apply volume knob actions through software or the system mixer."""

from __future__ import annotations

from .audio import DEFAULT_MIXER_CONTROLS, AudioError, VolumeState, change_volume, toggle_mute
from .audio import VOLUME_STEP_PERCENT as MIXER_VOLUME_STEP_PERCENT


DEFAULT_UNMUTE_VOLUME_PERCENT = 10
SOFTWARE_VOLUME_STEP_PERCENT = 5
FINE_VOLUME_LIMIT_PERCENT = 10
VOLUME_INPUTS = frozenset({"volume", "volume_click"})


def effective_muted(percent: int, muted: bool) -> bool:
    return muted or percent <= 0


def unmuted_volume(percent: int) -> VolumeState:
    return VolumeState(percent if percent > 0 else DEFAULT_UNMUTE_VOLUME_PERCENT)


def stepped_volume_percent(percent: int, steps: int) -> int:
    percent = min(max(percent, 0), 100)
    for _ in range(min(abs(steps), 100)):
        if steps > 0:
            step = 1 if percent < FINE_VOLUME_LIMIT_PERCENT else SOFTWARE_VOLUME_STEP_PERCENT
            percent = min(percent + step, 100)
        elif percent > FINE_VOLUME_LIMIT_PERCENT:
            percent = max(percent - SOFTWARE_VOLUME_STEP_PERCENT, FINE_VOLUME_LIMIT_PERCENT)
        else:
            percent = max(percent - 1, 0)
    return percent


def restored_audible_volume_state(controls: tuple[str, ...]) -> VolumeState:
    steps = max(1, DEFAULT_UNMUTE_VOLUME_PERCENT // MIXER_VOLUME_STEP_PERCENT)
    try:
        state = change_volume(steps, controls=controls)
    except AudioError:
        return unmuted_volume(0)
    if effective_muted(state.percent, state.muted):
        return unmuted_volume(0)
    return state


def apply_volume_input(
    event_name: str,
    steps: int,
    current: VolumeState,
    *,
    mixer_controls: tuple[str, ...] = DEFAULT_MIXER_CONTROLS,
    software_volume_only: bool = False,
) -> VolumeState:
    if not software_volume_only:
        try:
            if event_name == "volume_click":
                return _toggle_mixer_mute(current, mixer_controls)
            return change_volume(steps, controls=mixer_controls)
        except AudioError as error:
            print(f"Volume control failed: {error}")

    return _software_volume_for_input(event_name, steps, current)


def _software_volume_for_input(event_name: str, steps: int, current: VolumeState) -> VolumeState:
    if event_name == "volume_click":
        if effective_muted(current.percent, current.muted):
            return unmuted_volume(current.percent)
        return VolumeState(current.percent, muted=True)
    return VolumeState(stepped_volume_percent(current.percent, steps))


def _toggle_mixer_mute(current: VolumeState, controls: tuple[str, ...]) -> VolumeState:
    state = toggle_mute(controls=controls)
    if effective_muted(current.percent, current.muted) and effective_muted(
        state.percent, state.muted
    ):
        return restored_audible_volume_state(controls)
    return state
