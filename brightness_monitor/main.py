"""daemon that syncs MacBook keyboard brightness to Claude API usage.

full brightness = fresh window, lots of tokens left.
darkness = approaching the limit.
pulsing = almost out, <=threshold remaining.
periodic blink readout = flash the remaining percentage as digit blinks.
"""

from __future__ import annotations

import argparse
import logging
import math
import signal
import threading
import time
from typing import Optional

from brightness_monitor.brightness import (
    get_brightness,
    set_brightness,
    set_auto_brightness,
    suspend_idle_dimming,
)
from brightness_monitor.config import Config, load_config
from brightness_monitor.usage import get_token, fetch_usage, UsageData

log = logging.getLogger("brightness_monitor")

# pulse animation frame rate
PULSE_FRAME_SECONDS = 0.05  # ~20fps


class ShutdownHandler:
    """saves original keyboard state and restores it on exit."""

    def __init__(self):
        self.original_brightness: Optional[float] = None
        self._shutdown = False
        self._wake = threading.Event()

    @property
    def running(self) -> bool:
        return not self._shutdown

    def save_state(self) -> None:
        self.original_brightness = get_brightness()
        log.info(
            "saved original brightness: %(b).2f",
            {"b": self.original_brightness},
        )

    def restore_state(self) -> None:
        if self.original_brightness is not None:
            set_brightness(self.original_brightness)
            log.info(
                "restored brightness to %(b).2f",
                {"b": self.original_brightness},
            )
        suspend_idle_dimming(False)
        set_auto_brightness(True)
        log.info("re-enabled auto-brightness and idle dimming")

    def handle_signal(self, signum, frame) -> None:
        log.info("caught signal %(sig)s, shutting down", {"sig": signum})
        self._shutdown = True
        self._wake.set()

    def wake(self) -> None:
        """interrupt any in-progress sleep immediately."""
        self._wake.set()

    def interruptible_sleep(self, seconds: float) -> None:
        """sleep until timeout, shutdown, or wake() — whichever comes first."""
        self._wake.wait(timeout=seconds)
        self._wake.clear()


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
    handler: ShutdownHandler,
) -> None:
    """breathe keyboard brightness between 0 and max_level.

    uses a sine wave for smooth animation. runs for `duration`
    seconds or until shutdown is requested.

    the effect: as remaining tokens shrink, the pulse gets dimmer
    and dimmer — like a candle about to go out.
    """
    start = time.monotonic()
    while handler.running and (time.monotonic() - start) < duration:
        elapsed = time.monotonic() - start
        phase = (elapsed % period) / period
        # sine wave: 0 -> max_level -> 0 -> max_level ...
        level = max_level * (0.5 + 0.5 * math.sin(2 * math.pi * phase - math.pi / 2))
        set_brightness(level, fade_speed=fade_speed)
        time.sleep(PULSE_FRAME_SECONDS)


def blink_digit(
    digit: int,
    config: Config,
    handler: ShutdownHandler,
) -> None:
    """blink the keyboard `digit` times to represent a single digit.

    0 is shown as one long blink (twice the normal on-duration).
    """
    if not handler.running:
        return

    fade = config.readout.fade_speed

    if digit == 0:
        # long blink for zero
        set_brightness(1.0, fade_speed=fade)
        time.sleep(config.readout.blink_on * 2)
        set_brightness(0.0, fade_speed=fade)
        time.sleep(config.readout.blink_off)
        return

    for i in range(digit):
        if not handler.running:
            return
        set_brightness(1.0, fade_speed=fade)
        time.sleep(config.readout.blink_on)
        set_brightness(0.0, fade_speed=fade)
        time.sleep(config.readout.blink_off)


def blink_percentage_readout(
    remaining_percent: float,
    config: Config,
    handler: ShutdownHandler,
) -> None:
    """flash the remaining percentage as blink patterns.

    example: 24% remaining = 2 blinks, pause, 4 blinks.
    """
    clamped = max(0, min(99, int(remaining_percent)))
    tens = clamped // 10
    ones = clamped % 10

    log.info(
        "readout: %(pct)d%% remaining -> %(tens)d + %(ones)d blinks",
        {"pct": clamped, "tens": tens, "ones": ones},
    )

    fade = config.readout.fade_speed

    # go dark first so the readout is clearly separate from normal brightness
    set_brightness(0.0, fade_speed=fade)
    time.sleep(0.3)

    if config.readout.granularity == "tens":
        blink_digit(tens, config, handler)
    else:
        blink_digit(tens, config, handler)
        time.sleep(config.readout.digit_pause)
        blink_digit(ones, config, handler)

    # hold dark briefly, then end pause
    set_brightness(0.0, fade_speed=fade)
    time.sleep(config.readout.end_pause)


def format_status(usage: UsageData) -> str:
    """single-line summary of all usage windows."""
    parts = []
    for window in usage.windows:
        remaining = 100.0 - window.utilization
        label = window.name.replace("_", " ")
        parts.append(
            "%(label)s: %(remaining).1f%% left"
            % {"label": label, "remaining": remaining}
        )
    return " | ".join(parts)


def _readout_bucket(remaining: float, every_percent: float) -> int:
    """which threshold bucket does this remaining % fall into?

    with every_percent=5: 100->20, 95->19, 90->18, ... 5->1, 0->0.
    a readout fires when the bucket changes.
    """
    return int(remaining / every_percent)


def run_daemon(
    config: Config,
    dry_run: bool,
    token_override: Optional[str] = None,
) -> None:
    """main loop: poll usage, map to brightness, readout on threshold crossings."""
    handler = ShutdownHandler()
    signal.signal(signal.SIGINT, handler.handle_signal)
    signal.signal(signal.SIGTERM, handler.handle_signal)

    # SIGUSR1 triggers an immediate readout (for skhd hotkey)
    readout_requested = False

    def handle_usr1(signum, frame):
        nonlocal readout_requested
        log.info("SIGUSR1 received, readout requested")
        readout_requested = True
        handler.wake()

    signal.signal(signal.SIGUSR1, handle_usr1)

    # verify credentials exist at startup
    log.info("resolving Claude OAuth token")
    get_token(explicit_token=token_override)
    log.info("authenticated")

    if not dry_run:
        handler.save_state()
        suspend_idle_dimming(True)
        set_auto_brightness(False)
        log.info("took control of keyboard brightness")

    last_bucket: Optional[int] = None

    try:
        while handler.running:
            # re-read token each poll so we pick up Keychain refreshes
            try:
                token = get_token(explicit_token=token_override)
                usage = fetch_usage(token)
            except RuntimeError as error:
                log.warning(
                    "usage fetch failed, skipping: %(error)s",
                    {"error": error},
                )
                handler.interruptible_sleep(config.poll_interval)
                continue

            if config.window == "most_constrained":
                tracked = usage.most_constrained
            else:
                matched = [w for w in usage.windows if w.name == config.window]
                if not matched:
                    log.warning(
                        "window %(w)s not found in API response, "
                        "falling back to most constrained",
                        {"w": config.window},
                    )
                    tracked = usage.most_constrained
                else:
                    tracked = matched[0]

            remaining = 100.0 - tracked.utilization

            log.info(format_status(usage))

            # check if we crossed a percentage threshold since last readout
            current_bucket = _readout_bucket(
                remaining,
                config.readout.every_percent,
            )
            crossed_threshold = (
                last_bucket is not None
                and current_bucket != last_bucket
                and remaining <= config.readout.threshold
            )
            # first poll always fires a readout if within threshold
            first_poll = last_bucket is None and remaining <= config.readout.threshold

            should_readout = crossed_threshold or first_poll or readout_requested
            readout_requested = False

            if should_readout:
                clamped = max(0, min(99, int(remaining)))
                if dry_run:
                    log.info(
                        "readout (dry): %(pct)d%% -> %(tens)d + %(ones)d blinks",
                        {
                            "pct": clamped,
                            "tens": clamped // 10,
                            "ones": clamped % 10,
                        },
                    )
                else:
                    blink_percentage_readout(remaining, config, handler)

            last_bucket = current_bucket

            if remaining <= config.pulse_threshold:
                # pulse between 0 and the remaining percentage
                pulse_max = remaining / 100.0
                log.info(
                    "pulse mode on %(window)s: %(remaining).1f%% left, "
                    "breathing 0-%(max).3f",
                    {
                        "window": tracked.name,
                        "remaining": remaining,
                        "max": pulse_max,
                    },
                )
                if dry_run:
                    handler.interruptible_sleep(config.poll_interval)
                else:
                    pulse_brightness(
                        pulse_max,
                        config.poll_interval,
                        config.pulse_period,
                        config.fade_speed,
                        handler,
                    )

            else:
                brightness = utilization_to_brightness(
                    tracked.utilization,
                    config.min_brightness,
                )
                log.info(
                    "steady: %(window)s %(util).1f%% used -> brightness %(b).3f",
                    {
                        "window": tracked.name,
                        "util": tracked.utilization,
                        "b": brightness,
                    },
                )
                if not dry_run:
                    set_brightness(brightness, fade_speed=config.fade_speed)
                handler.interruptible_sleep(config.poll_interval)

    finally:
        if not dry_run:
            handler.restore_state()
        log.info("shutdown complete")


def main():
    parser = argparse.ArgumentParser(
        description="sync MacBook keyboard brightness to Claude API usage",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="path to config.yaml (default: config.yaml in project root)",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="OAuth token (overrides env var and Keychain lookup)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log what would happen without touching brightness",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="enable debug logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    from pathlib import Path

    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)

    log.info(
        "config: window=%(window)s, poll=%(poll)ds, fade=%(fade)d, "
        "pulse<%(pulse).0f%%, readout every %(every).0f%% below %(thresh).0f%%",
        {
            "window": config.window,
            "poll": config.poll_interval,
            "fade": config.fade_speed,
            "pulse": config.pulse_threshold,
            "every": config.readout.every_percent,
            "thresh": config.readout.threshold,
        },
    )

    run_daemon(
        config=config,
        dry_run=args.dry_run,
        token_override=args.token,
    )


if __name__ == "__main__":
    main()
