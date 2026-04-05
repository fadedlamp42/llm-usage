"""keyboard backlight output for llm-usage.

controls MacBook keyboard brightness to reflect LLM API usage:
  - steady brightness mapped to utilization percentage
  - pulse/breathing animation when running critically low
  - blink readout encoding remaining % as digit blink patterns
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from prism.logging import get_logger

from llm_usage.brightness import set_brightness

if TYPE_CHECKING:
    from llm_usage.config import KeyboardConfig

logger = get_logger()

# pulse animation frame rate
PULSE_FRAME_SECONDS = 0.05  # ~20fps


def utilization_to_brightness(
    utilization: float,
    min_brightness: float,
) -> float:
    """map utilization percentage to brightness level.

    0% utilized   -> 1.0 (full brightness, fresh window)
    100% utilized -> min_brightness (near-dark)
    """
    remaining_fraction = (100.0 - utilization) / 100.0
    return min_brightness + remaining_fraction * (1.0 - min_brightness)


def pulse_brightness(
    max_level: float,
    duration: float,
    period: float,
    fade_speed: int,
    running_check,
) -> None:
    """breathe keyboard brightness between 0 and max_level.

    uses a sine wave for smooth animation. runs for `duration`
    seconds or until running_check() returns False.

    the effect: as remaining tokens shrink, the pulse gets dimmer
    and dimmer — like a candle about to go out.
    """
    start = time.monotonic()
    while running_check() and (time.monotonic() - start) < duration:
        elapsed = time.monotonic() - start
        phase = (elapsed % period) / period
        # sine wave: 0 -> max_level -> 0 -> max_level ...
        level = max_level * (0.5 + 0.5 * math.sin(2 * math.pi * phase - math.pi / 2))
        set_brightness(level, fade_speed=fade_speed)
        time.sleep(PULSE_FRAME_SECONDS)


def blink_digit(
    digit: int,
    keyboard_config: KeyboardConfig,
    running_check,
) -> None:
    """blink the keyboard `digit` times to represent a single digit.

    0 is shown as one long blink (twice the normal on-duration).
    """
    if not running_check():
        return

    readout = keyboard_config.readout
    fade = readout.fade_speed

    if digit == 0:
        # long blink for zero
        set_brightness(1.0, fade_speed=fade)
        time.sleep(readout.blink_on * 2)
        set_brightness(0.0, fade_speed=fade)
        time.sleep(readout.blink_off)
        return

    for _i in range(digit):
        if not running_check():
            return
        set_brightness(1.0, fade_speed=fade)
        time.sleep(readout.blink_on)
        set_brightness(0.0, fade_speed=fade)
        time.sleep(readout.blink_off)


def blink_percentage_readout(
    remaining_percent: float,
    keyboard_config: KeyboardConfig,
    running_check,
) -> None:
    """flash the remaining percentage as blink patterns.

    example: 24% remaining = 2 blinks, pause, 4 blinks.
    """
    clamped = max(0, min(99, int(remaining_percent)))
    tens = clamped // 10
    ones = clamped % 10

    logger.info("readout", remaining_percent=clamped, tens=tens, ones=ones)

    readout = keyboard_config.readout
    fade = readout.fade_speed

    # go dark first so the readout is clearly separate from normal brightness
    set_brightness(0.0, fade_speed=fade)
    time.sleep(0.3)

    if readout.granularity == "tens":
        blink_digit(tens, keyboard_config, running_check)
    else:
        blink_digit(tens, keyboard_config, running_check)
        time.sleep(readout.digit_pause)
        blink_digit(ones, keyboard_config, running_check)

    # hold dark briefly, then end pause
    set_brightness(0.0, fade_speed=fade)
    time.sleep(readout.end_pause)
