"""daemon that syncs MacBook keyboard brightness to Claude API usage.

full brightness = fresh window, lots of tokens left.
darkness = approaching the limit.
pulsing = almost out, <=threshold remaining.
periodic blink readout = flash the remaining percentage as digit blinks.
whispered readout = spoken hourly percentage via chatterbox.
full voice report = thorough status via kokoro covering all windows.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import signal
import subprocess
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
from brightness_monitor.keyboard import (
    utilization_to_brightness,
    pulse_brightness,
    blink_percentage_readout,
)
from brightness_monitor.speech import (
    speak_hourly_status,
    speak_full_status,
    announce_auth_expired,
    announce_auth_login_started,
    announce_auth_login_result,
)
from brightness_monitor.storage import (
    initialize_database,
    record_poll,
    calculate_burn_rate,
)
from brightness_monitor.usage import get_token, fetch_usage, AuthExpiredError, UsageData

log = logging.getLogger("brightness_monitor")


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


def format_status(usage: UsageData) -> str:
    """single-line summary of all usage windows for logging."""
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


def _attempt_reauth() -> bool:
    """run `claude auth login` and return whether it succeeded.

    blocks until the OAuth flow completes (user clicks through browser).
    returns False if claude CLI isn't found or the process exits non-zero.
    """
    claude_path = shutil.which("claude")
    if not claude_path:
        log.error("claude CLI not found in PATH, cannot re-authenticate")
        return False

    log.info("starting claude auth login")
    try:
        result = subprocess.run(
            [claude_path, "auth", "login"],
            timeout=120,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log.info("claude auth login succeeded")
            return True

        log.warning(
            "claude auth login failed (exit %(code)d): %(stderr)s",
            {"code": result.returncode, "stderr": result.stderr.strip()},
        )
        return False
    except subprocess.TimeoutExpired:
        log.warning("claude auth login timed out after 120s")
        return False
    except Exception as error:
        log.warning(
            "claude auth login error: %(error)s",
            {"error": error},
        )
        return False


def run_daemon(
    config: Config,
    dry_run: bool,
    token_override: Optional[str] = None,
) -> None:
    """main loop: poll usage, map to brightness, readout on threshold crossings."""
    handler = ShutdownHandler()
    signal.signal(signal.SIGINT, handler.handle_signal)
    signal.signal(signal.SIGTERM, handler.handle_signal)

    keyboard = config.output.keyboard

    # SIGUSR1 triggers an immediate readout (cmd+shift+b)
    # speech: whispered hourly, keyboard: blink readout
    # when auth is expired: triggers re-authentication via claude auth login
    readout_requested = False

    def handle_usr1(signum, frame):
        nonlocal readout_requested
        log.info("SIGUSR1 received, readout requested")
        readout_requested = True
        handler.wake()

    signal.signal(signal.SIGUSR1, handle_usr1)

    # SIGUSR2 triggers a full spoken report via cute-say (cmd+shift+alt+b)
    voice_readout_requested = False

    def handle_usr2(signum, frame):
        nonlocal voice_readout_requested
        log.info("SIGUSR2 received, voice readout requested")
        voice_readout_requested = True
        handler.wake()

    signal.signal(signal.SIGUSR2, handle_usr2)

    # verify credentials exist at startup
    log.info("resolving Claude OAuth token")
    get_token(explicit_token=token_override)
    log.info("authenticated")

    # initialize usage history database
    db = initialize_database()

    if not dry_run and keyboard.enabled:
        handler.save_state()
        suspend_idle_dimming(True)
        set_auto_brightness(False)
        log.info("took control of keyboard brightness")

    last_bucket: Optional[int] = None
    last_fetch_time: float = 0.0
    cached_usage: Optional[UsageData] = None
    auth_expired = False

    try:
        while handler.running:
            # when auth is expired, wait for SIGUSR1 to trigger re-login
            if auth_expired:
                if readout_requested:
                    readout_requested = False
                    if config.output.speech:
                        announce_auth_login_started()
                    success = _attempt_reauth()
                    if config.output.speech:
                        announce_auth_login_result(success)
                    if success:
                        auth_expired = False
                        log.info("auth restored, resuming normal operation")
                        # fall through to poll immediately
                    else:
                        handler.interruptible_sleep(config.poll_interval)
                        continue
                else:
                    handler.interruptible_sleep(config.poll_interval)
                    continue

            # debounce API requests — reuse cached data if polled recently.
            # prevents spamming the usage API when pressing the hotkey
            # multiple times to re-listen to a readout.
            seconds_since_fetch = time.monotonic() - last_fetch_time
            if cached_usage is not None and seconds_since_fetch < config.poll_interval:
                log.debug(
                    "using cached usage data (%(age).0fs old)",
                    {"age": seconds_since_fetch},
                )
                usage = cached_usage
            else:
                try:
                    token = get_token(explicit_token=token_override)
                    usage = fetch_usage(token)
                except AuthExpiredError:
                    log.warning("auth token expired")
                    if not auth_expired and config.output.speech:
                        announce_auth_expired()
                    auth_expired = True
                    handler.interruptible_sleep(config.poll_interval)
                    continue
                except RuntimeError as error:
                    log.warning(
                        "usage fetch failed, skipping: %(error)s",
                        {"error": error},
                    )
                    handler.interruptible_sleep(config.poll_interval)
                    continue

                last_fetch_time = time.monotonic()
                cached_usage = usage

                # only record fresh polls to sqlite, not cached replays
                try:
                    record_poll(db, usage)
                except Exception as error:
                    log.warning(
                        "failed to record poll to database: %(error)s",
                        {"error": error},
                    )
                handler.interruptible_sleep(config.poll_interval)
                continue

            # record every poll to sqlite for usage history
            try:
                record_poll(db, usage)
            except Exception as error:
                log.warning(
                    "failed to record poll to database: %(error)s",
                    {"error": error},
                )

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
                keyboard.readout.every_percent,
            )
            crossed_threshold = (
                last_bucket is not None
                and current_bucket != last_bucket
                and remaining <= keyboard.readout.threshold
            )
            # first poll always fires a readout if within threshold
            first_poll = last_bucket is None and remaining <= keyboard.readout.threshold

            should_readout = crossed_threshold or first_poll or readout_requested
            readout_requested = False

            if should_readout:
                clamped = max(0, min(99, int(remaining)))

                # hourly status with pace observation via naturalized chatterbox
                if config.output.speech:
                    burn_rate = calculate_burn_rate(
                        db,
                        tracked.name,
                        tracked.resets_at,
                    )
                    speak_hourly_status(usage, burn_rate)

                # blink readout via keyboard backlight
                if keyboard.enabled:
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
                        blink_percentage_readout(
                            remaining, keyboard, lambda: handler.running
                        )

            # full voice report via kokoro (independent of standard readout)
            if voice_readout_requested:
                voice_readout_requested = False
                if config.output.speech:
                    speak_full_status(usage)

            last_bucket = current_bucket

            # keyboard brightness control — only when keyboard output is enabled
            if keyboard.enabled:
                if remaining <= keyboard.pulse_threshold:
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
                            keyboard.pulse_period,
                            keyboard.fade_speed,
                            lambda: handler.running,
                        )

                else:
                    brightness = utilization_to_brightness(
                        tracked.utilization,
                        keyboard.min_brightness,
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
                        set_brightness(brightness, fade_speed=keyboard.fade_speed)
                    handler.interruptible_sleep(config.poll_interval)

            else:
                # no keyboard output — just sleep until next poll
                handler.interruptible_sleep(config.poll_interval)

    finally:
        if not dry_run and keyboard.enabled:
            handler.restore_state()
        db.close()
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

    kb = config.output.keyboard
    log.info(
        "config: window=%(window)s, poll=%(poll)ds, "
        "output: speech=%(speech)s keyboard=%(keyboard)s",
        {
            "window": config.window,
            "poll": config.poll_interval,
            "speech": config.output.speech,
            "keyboard": kb.enabled,
        },
    )
    if kb.enabled:
        log.info(
            "keyboard: fade=%(fade)d, pulse<%(pulse).0f%%, "
            "readout every %(every).0f%% below %(thresh).0f%%",
            {
                "fade": kb.fade_speed,
                "pulse": kb.pulse_threshold,
                "every": kb.readout.every_percent,
                "thresh": kb.readout.threshold,
            },
        )

    run_daemon(
        config=config,
        dry_run=args.dry_run,
        token_override=args.token,
    )


if __name__ == "__main__":
    main()
