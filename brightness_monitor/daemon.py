"""core daemon loop: poll usage, map to brightness, handle auth lifecycle.

ties together usage polling, keyboard brightness, speech readouts,
and re-authentication into a single event loop managed by ShutdownHandler.
"""

from __future__ import annotations

import signal
import threading
import time
from typing import TYPE_CHECKING

from prism.logging import get_logger

from brightness_monitor.auth import REAUTH_INTERVAL_SECONDS, attempt_reauth
from brightness_monitor.brightness import (
    get_brightness,
    set_auto_brightness,
    set_brightness,
    suspend_idle_dimming,
)
from brightness_monitor.keyboard import (
    blink_percentage_readout,
    pulse_brightness,
    utilization_to_brightness,
)
from brightness_monitor.speech import (
    announce_auth_expired,
    announce_auth_login_result,
    announce_auth_login_started,
    speak_full_status,
    speak_hourly_status,
)
from brightness_monitor.speech import (
    configure as configure_speech,
)
from brightness_monitor.storage import (
    calculate_burn_rate,
    initialize_database,
    record_poll,
)
from brightness_monitor.usage import AuthExpiredError, UsageData, fetch_usage, get_token

if TYPE_CHECKING:
    from brightness_monitor.config import Config

logger = get_logger()


class ShutdownHandler:
    """saves original keyboard state and restores it on exit."""

    def __init__(self):
        self.original_brightness: float | None = None
        self._shutdown = False
        self._wake = threading.Event()

    @property
    def running(self) -> bool:
        return not self._shutdown

    def save_state(self) -> None:
        self.original_brightness = get_brightness()
        logger.info("saved original brightness", brightness=self.original_brightness)

    def restore_state(self) -> None:
        if self.original_brightness is not None:
            set_brightness(self.original_brightness)
            logger.info("restored brightness", brightness=self.original_brightness)
        suspend_idle_dimming(False)
        set_auto_brightness(True)
        logger.info("re-enabled auto-brightness and idle dimming")

    def handle_signal(self, signum, frame) -> None:
        logger.info("caught signal, shutting down", signal=signum)
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
            "%(label)s: %(remaining).1f%% left" % {"label": label, "remaining": remaining}
        )
    return " | ".join(parts)


def _readout_bucket(remaining: float, every_percent: float) -> int:
    """which threshold bucket does this remaining % fall into?

    with every_percent=5: 100->20, 95->19, 90->18, ... 5->1, 0->0.
    a readout fires when the bucket changes.
    """
    return int(remaining / every_percent)


def _validate_auth_at_startup(
    handler: ShutdownHandler,
    config: Config,
    token_override: str | None,
) -> None:
    """verify credentials actually work before entering the main loop.

    not just that a token exists, but that it's accepted by the API.
    auto-reauths if expired so the daemon never starts with a dead token.
    """
    logger.info("validating Claude OAuth token")
    while handler.running:
        try:
            token = get_token(explicit_token=token_override)
            fetch_usage(token)
            logger.info("auth validated, token is live")
            return
        except AuthExpiredError:
            logger.warning("token expired at startup, attempting reauth")
            if config.output.speech:
                announce_auth_login_started()
            success = attempt_reauth()
            if config.output.speech:
                announce_auth_login_result(success)
            if success:
                logger.info("startup reauth succeeded")
                continue  # re-validate with fresh token
            logger.warning(
                "startup reauth failed, retrying",
                interval=REAUTH_INTERVAL_SECONDS,
            )
            handler.interruptible_sleep(REAUTH_INTERVAL_SECONDS)
        except RuntimeError as error:
            logger.warning(
                "startup auth check failed, retrying",
                error=str(error),
                interval=REAUTH_INTERVAL_SECONDS,
            )
            handler.interruptible_sleep(REAUTH_INTERVAL_SECONDS)


def run_daemon(
    config: Config,
    dry_run: bool,
    token_override: str | None = None,
) -> None:
    """main loop: poll usage, map to brightness, readout on threshold crossings."""
    # configure speech module for sttts mic coordination
    configure_speech(
        sttts_relay_url=config.sttts.relay_url if config.sttts.enabled else None,
    )

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
        logger.info("SIGUSR1 received, readout requested")
        readout_requested = True
        handler.wake()

    signal.signal(signal.SIGUSR1, handle_usr1)

    # SIGUSR2 triggers a full spoken report via cute-say (cmd+shift+alt+b)
    voice_readout_requested = False

    def handle_usr2(signum, frame):
        nonlocal voice_readout_requested
        logger.info("SIGUSR2 received, voice readout requested")
        voice_readout_requested = True
        handler.wake()

    signal.signal(signal.SIGUSR2, handle_usr2)

    _validate_auth_at_startup(handler, config, token_override)

    # initialize usage history database
    db = initialize_database()

    if not dry_run and keyboard.enabled:
        handler.save_state()
        suspend_idle_dimming(True)
        set_auto_brightness(False)
        logger.info("took control of keyboard brightness")

    last_bucket: int | None = None
    last_fetch_time: float = 0.0
    cached_usage: UsageData | None = None
    auth_expired = False
    last_reauth_attempt: float = 0.0

    try:
        while handler.running:
            # when auth is expired, attempt re-login either on SIGUSR1
            # (immediate) or automatically every REAUTH_INTERVAL_SECONDS
            # to minimize data gaps in UsageDB.
            if auth_expired:
                seconds_since_reauth = time.monotonic() - last_reauth_attempt
                should_auto_reauth = seconds_since_reauth >= REAUTH_INTERVAL_SECONDS

                if readout_requested or should_auto_reauth:
                    readout_requested = False
                    last_reauth_attempt = time.monotonic()
                    if config.output.speech:
                        announce_auth_login_started()
                    success = attempt_reauth()
                    if config.output.speech:
                        announce_auth_login_result(success)
                    if success:
                        auth_expired = False
                        logger.info("auth restored, resuming normal operation")
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
                logger.debug("using cached usage data", age_seconds=round(seconds_since_fetch))
                usage = cached_usage
            else:
                try:
                    token = get_token(explicit_token=token_override)
                    usage = fetch_usage(token)
                except AuthExpiredError:
                    logger.warning("auth token expired")
                    if not auth_expired:
                        if config.output.speech:
                            announce_auth_expired()
                        # schedule first auto-reauth attempt in REAUTH_INTERVAL_SECONDS
                        last_reauth_attempt = time.monotonic()
                    auth_expired = True
                    handler.interruptible_sleep(config.poll_interval)
                    continue
                except RuntimeError as error:
                    logger.warning("usage fetch failed, skipping", error=str(error))
                    handler.interruptible_sleep(config.poll_interval)
                    continue

                last_fetch_time = time.monotonic()
                cached_usage = usage

                # record fresh polls to sqlite for usage history
                try:
                    record_poll(db, usage)
                except Exception as error:
                    logger.warning("failed to record poll to database", error=str(error))

            if config.window == "most_constrained":
                tracked = usage.most_constrained
            else:
                matched = [w for w in usage.windows if w.name == config.window]
                if not matched:
                    logger.warning(
                        "window not found in API response, falling back to most constrained",
                        window=config.window,
                    )
                    tracked = usage.most_constrained
                else:
                    tracked = matched[0]

            remaining = 100.0 - tracked.utilization

            logger.info(format_status(usage))

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
                        logger.info(
                            "readout (dry run)",
                            percent=clamped,
                            tens=clamped // 10,
                            ones=clamped % 10,
                        )
                    else:
                        blink_percentage_readout(remaining, keyboard, lambda: handler.running)

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
                    logger.info(
                        "pulse mode",
                        window=tracked.name,
                        remaining=round(remaining, 1),
                        pulse_max=round(pulse_max, 3),
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
                    logger.info(
                        "steady brightness",
                        window=tracked.name,
                        utilization=round(tracked.utilization, 1),
                        brightness=round(brightness, 3),
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
        logger.info("shutdown complete")
