"""core daemon loop: poll usage, map to brightness, handle auth lifecycle.

ties together provider-based usage polling, keyboard brightness,
speech readouts, and re-authentication into a single event loop
managed by ShutdownHandler.
"""

from __future__ import annotations

import signal
import threading
import time
from typing import TYPE_CHECKING

from prism.logging import get_logger
from prism.mac.screen import is_screen_locked

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
from brightness_monitor.providers import create_usage_provider
from brightness_monitor.speech import (
    announce_auth_expired,
    announce_auth_login_result,
    announce_auth_login_started,
    speak_full_status,
    speak_hourly_status,
    suggest_account_switch,
)
from brightness_monitor.speech import (
    configure as configure_speech,
)
from brightness_monitor.storage import (
    calculate_burn_rate,
    get_alternative_account_utilizations,
    initialize_database,
    record_poll,
)
from brightness_monitor.usage import AuthExpiredError, UsageData

if TYPE_CHECKING:
    from brightness_monitor.config import Config
    from brightness_monitor.providers import UsageProvider

logger = get_logger()

AUTH_RETRY_INTERVAL_SECONDS = 300


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
    if usage.account_email:
        parts.append(usage.account_email)
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


def _validate_provider_at_startup(
    handler: ShutdownHandler,
    config: Config,
    provider: UsageProvider,
) -> None:
    """verify the configured usage provider before entering the main loop."""
    logger.info("validating usage provider", provider=provider.provider_name)
    while handler.running:
        # don't attempt auth validation while the screen is locked —
        # avoids opening browser tabs nobody's around to complete
        if is_screen_locked():
            logger.debug("screen locked, deferring auth validation")
            handler.interruptible_sleep(AUTH_RETRY_INTERVAL_SECONDS)
            continue

        try:
            provider.fetch_usage()
            logger.info("usage provider validated", provider=provider.provider_name)
            return
        except AuthExpiredError:
            logger.warning(
                "provider auth expired at startup, attempting reauth",
                provider=provider.provider_name,
            )
            if config.output.speech:
                announce_auth_login_started()
            success = provider.attempt_reauth()
            if config.output.speech:
                announce_auth_login_result(success)
            if success:
                logger.info("startup reauth succeeded")
                continue  # re-validate with fresh token
            logger.warning(
                "startup reauth failed, retrying",
                provider=provider.provider_name,
                interval=AUTH_RETRY_INTERVAL_SECONDS,
            )
            handler.interruptible_sleep(AUTH_RETRY_INTERVAL_SECONDS)
        except RuntimeError as error:
            logger.warning(
                "startup usage check failed, retrying",
                provider=provider.provider_name,
                error=str(error),
                interval=AUTH_RETRY_INTERVAL_SECONDS,
            )
            handler.interruptible_sleep(AUTH_RETRY_INTERVAL_SECONDS)


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

    provider = create_usage_provider(config, token_override=token_override)
    logger.info("using usage provider", provider=provider.provider_name)

    keyboard = config.output.keyboard

    # SIGUSR1 triggers an immediate readout (cmd+shift+b)
    # speech: whispered hourly, keyboard: blink readout
    # when auth is expired: triggers provider re-authentication if available
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

    _validate_provider_at_startup(handler, config, provider)

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
    screen_was_locked = False
    switch_suggested = False  # debounce: only suggest once per threshold crossing

    try:
        while handler.running:
            # suspend everything while the screen is locked — no polling,
            # no auth attempts, no speech, no keyboard changes. prevents
            # re-auth from opening dozens of browser tabs overnight.
            screen_locked = is_screen_locked()
            if screen_locked:
                if not screen_was_locked:
                    logger.info("screen locked, suspending")
                    # restore keyboard to normal so it behaves naturally while locked
                    if not dry_run and keyboard.enabled:
                        handler.restore_state()
                    screen_was_locked = True
                handler.interruptible_sleep(config.poll_interval)
                continue

            if screen_was_locked:
                logger.info("screen unlocked, resuming")
                if not dry_run and keyboard.enabled:
                    handler.save_state()
                    suspend_idle_dimming(True)
                    set_auto_brightness(False)
                screen_was_locked = False

            # when auth is expired, attempt re-login either on SIGUSR1
            # (immediate) or automatically every AUTH_RETRY_INTERVAL_SECONDS
            # to minimize data gaps in UsageDB.
            if auth_expired:
                seconds_since_reauth = time.monotonic() - last_reauth_attempt
                should_auto_reauth = seconds_since_reauth >= AUTH_RETRY_INTERVAL_SECONDS

                if readout_requested or should_auto_reauth:
                    readout_requested = False
                    last_reauth_attempt = time.monotonic()
                    if config.output.speech:
                        announce_auth_login_started()
                    success = provider.attempt_reauth()
                    if config.output.speech:
                        announce_auth_login_result(success)
                    if success:
                        auth_expired = False
                        logger.info(
                            "auth restored, resuming normal operation",
                            provider=provider.provider_name,
                        )
                        # fall through to poll immediately
                    else:
                        handler.interruptible_sleep(config.poll_interval)
                        continue
                else:
                    handler.interruptible_sleep(config.poll_interval)
                    continue

            # debounce API requests — reuse cached data if polled recently.
            # prevents spamming the usage provider when pressing the hotkey
            # multiple times to re-listen to a readout.
            seconds_since_fetch = time.monotonic() - last_fetch_time
            if cached_usage is not None and seconds_since_fetch < config.poll_interval:
                logger.debug("using cached usage data", age_seconds=round(seconds_since_fetch))
                usage = cached_usage
            else:
                try:
                    usage = provider.fetch_usage()
                except AuthExpiredError:
                    logger.warning("provider auth expired", provider=provider.provider_name)
                    if not auth_expired:
                        if config.output.speech:
                            announce_auth_expired()
                        # schedule first auto-reauth attempt in AUTH_RETRY_INTERVAL_SECONDS
                        last_reauth_attempt = time.monotonic()
                    auth_expired = True
                    handler.interruptible_sleep(config.poll_interval)
                    continue
                except RuntimeError as error:
                    logger.warning(
                        "usage fetch failed, skipping",
                        provider=provider.provider_name,
                        error=str(error),
                    )
                    handler.interruptible_sleep(config.poll_interval)
                    continue

                last_fetch_time = time.monotonic()
                cached_usage = usage

                # record fresh polls to sqlite for usage history
                try:
                    record_poll(db, usage, provider_name=provider.provider_name)
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

            # account switch suggestion — fires once when crossing the
            # threshold, resets when utilization drops back below it
            if config.accounts and config.output.speech and usage.account_email:
                over_threshold = tracked.utilization >= config.switch_threshold
                if over_threshold and not switch_suggested:
                    switch_suggested = True
                    alternatives = get_alternative_account_utilizations(
                        db,
                        tracked.name,
                        usage.account_email,
                        config.accounts,
                    )
                    known_emails = {a.account_email for a in alternatives}
                    unknown_emails = [
                        email
                        for email in config.accounts
                        if email != usage.account_email and email not in known_emails
                    ]
                    suggest_account_switch(alternatives, unknown_emails)
                if not over_threshold:
                    switch_suggested = False

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
                        provider.provider_name,
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
