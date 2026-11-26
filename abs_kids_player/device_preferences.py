"""Apply a single device preference change to the latest saved configuration."""

from dataclasses import replace

from .config import (
    SCREEN_OFF_DELAYS,
    AppConfig,
    update_config,
    valid_clock_format,
    valid_feedback_volume,
    valid_playback_speed,
    valid_screen_off_hours,
    valid_screen_saver_dim_percent,
    valid_screen_saver_mode,
    valid_sleep_timer_minutes,
)


def save_click_setting(enabled: bool | None = None, *, volume_percent: object = None) -> AppConfig:
    _, updated = update_config(
        lambda config: replace(
            config,
            control_click_enabled=config.control_click_enabled if enabled is None else enabled,
            control_click_volume_percent=valid_feedback_volume(
                volume_percent, config.control_click_volume_percent
            ),
        )
    )
    return updated


def save_spoken_navigation_setting(
    enabled: bool | None = None, *, volume_percent: object = None
) -> AppConfig:
    _, updated = update_config(
        lambda config: replace(
            config,
            spoken_navigation_enabled=config.spoken_navigation_enabled
            if enabled is None
            else enabled,
            spoken_navigation_volume_percent=valid_feedback_volume(
                volume_percent, config.spoken_navigation_volume_percent
            ),
        )
    )
    return updated


def save_playback_speed_setting(speed: object) -> AppConfig:
    _, updated = update_config(
        lambda config: replace(config, playback_speed=valid_playback_speed(speed))
    )
    return updated


def save_screen_saver_setting(
    mode: str,
    dim_percent: int | str | None = None,
    *,
    clock_format: str | None = None,
    screen_off_minutes: str | None = None,
    screen_off_hours: tuple[str, str] | None = None,
) -> AppConfig:
    if screen_off_minutes is not None and screen_off_minutes not in {str(n) for n in SCREEN_OFF_DELAYS}:
        raise ValueError("Choose one of the available idle times.")
    if screen_off_hours is not None and not valid_screen_off_hours(*screen_off_hours):
        raise ValueError("Choose different start and end times, or clear both to turn off the schedule.")
    _, updated = update_config(
        lambda config: replace(
            config,
            screen_saver_mode=valid_screen_saver_mode(mode),
            screen_off_minutes=config.screen_off_minutes if screen_off_minutes is None else int(screen_off_minutes),
            screen_off_start=config.screen_off_start if screen_off_hours is None else screen_off_hours[0],
            screen_off_end=config.screen_off_end if screen_off_hours is None else screen_off_hours[1],
            screen_saver_dim_percent=(
                config.screen_saver_dim_percent
                if dim_percent is None
                else valid_screen_saver_dim_percent(dim_percent)
            ),
            screen_saver_clock_format=(
                config.screen_saver_clock_format
                if clock_format is None
                else valid_clock_format(clock_format)
            ),
        )
    )
    return updated


def save_sleep_timer_setting(
    enabled: bool | None = None,
    minutes: int | None = None,
) -> AppConfig:
    _, updated = update_config(
        lambda config: replace(
            config,
            sleep_timer_enabled=config.sleep_timer_enabled if enabled is None else enabled,
            sleep_timer_minutes=(
                config.sleep_timer_minutes
                if minutes is None
                else valid_sleep_timer_minutes(minutes)
            ),
        )
    )
    return updated
