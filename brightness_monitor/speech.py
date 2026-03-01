"""voice output for brightness-monitor via cute-say.

three modes:
  - hourly status: remaining %, reset time, and pace observation via naturalized chatterbox
  - full report: thorough status via kokoro at 1.4x speed covering all windows
  - auth alerts: notify about expired tokens and prompt for re-login
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.error import URLError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from brightness_monitor.storage import BurnRate
    from brightness_monitor.usage import UsageData

log = logging.getLogger(__name__)

# inter-service coordination with sttts
_sttts_relay_url: str | None = None
_MIC_POLL_INTERVAL = 0.5  # seconds between mic-status checks
_MIC_WAIT_TIMEOUT = 30.0  # max seconds to wait for mic idle


def configure(sttts_relay_url: str | None = None) -> None:
    """set module-level config for sttts mic coordination."""
    global _sttts_relay_url
    _sttts_relay_url = sttts_relay_url
    if _sttts_relay_url:
        log.info(
            "speech will defer to sttts mic at %(url)s",
            {"url": _sttts_relay_url},
        )


def _is_mic_capturing() -> bool | None:
    """check if sttts mic is currently capturing.

    returns True/False for mic state, or None if sttts is unreachable.
    """
    if not _sttts_relay_url:
        return None

    url = "%(base)s/mic-status" % {"base": _sttts_relay_url.rstrip("/")}
    try:
        request = Request(url)
        with urlopen(request, timeout=1.0) as response:
            data = json.loads(response.read())
            return data.get("capturing", False)
    except (URLError, OSError, json.JSONDecodeError, KeyError):
        return None


def _wait_for_mic_idle() -> None:
    """block until sttts mic capture is inactive.

    polls the sttts relay's /mic-status endpoint. returns immediately if:
    - no relay URL configured
    - sttts is unreachable
    - mic is not capturing
    - timeout exceeded
    """
    if not _sttts_relay_url:
        return

    deadline = time.monotonic() + _MIC_WAIT_TIMEOUT

    while time.monotonic() < deadline:
        capturing = _is_mic_capturing()

        # None = unreachable, False = idle — either way, proceed
        if capturing is not True:
            return

        remaining = deadline - time.monotonic()
        log.debug(
            "mic active, delaying speech (%(remaining).1fs remaining)",
            {"remaining": remaining},
        )
        time.sleep(_MIC_POLL_INTERVAL)

    log.warning(
        "mic-idle wait timed out after %(timeout).0fs, speaking anyway",
        {"timeout": _MIC_WAIT_TIMEOUT},
    )


def _format_relative_time(target: datetime | None) -> str:
    """format a datetime as natural spoken relative time.

    returns phrases like "in about an hour", "in 3 days", "tomorrow".
    returns empty string if target is None.
    """
    if target is None:
        return ""

    now = datetime.now(tz=target.tzinfo)
    total_seconds = (target - now).total_seconds()

    if total_seconds <= 0:
        return "any moment now"

    minutes = total_seconds / 60
    hours = total_seconds / 3600
    days = total_seconds / 86400

    if minutes < 2:
        return "in about a minute"
    if hours < 1:
        return "in %d minutes" % int(minutes)
    if hours < 2:
        return "in about an hour"
    if hours < 24:
        return "in %d hours" % int(hours)
    if days < 2:
        return "tomorrow"

    return "in %d days" % int(days)


def format_voice_status(usage: UsageData) -> str:
    """format a thorough spoken status update for cute-say.

    includes: hourly remaining (for verifying blink readouts), weekly remaining,
    per-model breakdown (opus/sonnet), and reset times for all windows.
    plain delivery, no paralinguistic tags.
    """
    windows_by_name = {w.name: w for w in usage.windows}

    five_hour = windows_by_name.get("five_hour")
    seven_day = windows_by_name.get("seven_day")
    opus = windows_by_name.get("seven_day_opus")
    sonnet = windows_by_name.get("seven_day_sonnet")

    parts = []

    # hourly first — this is what the keyboard blinks show, so state it
    # clearly so the user can verify what they just saw
    if five_hour:
        hr_left = int(100 - five_hour.utilization)
        reset = _format_relative_time(five_hour.resets_at)
        fragment = "hourly has %d percent left" % hr_left
        if reset:
            fragment += ", resets %s" % reset
        parts.append(fragment)

    # weekly aggregate
    if seven_day:
        wk_left = int(100 - seven_day.utilization)
        reset = _format_relative_time(seven_day.resets_at)
        fragment = "weekly has %d percent left" % wk_left
        if reset:
            fragment += ", resets %s" % reset
        parts.append(fragment)

    # per-model breakdown when available
    model_bits = []
    if opus:
        model_bits.append("opus at %d" % int(100 - opus.utilization))
    if sonnet:
        model_bits.append("sonnet at %d" % int(100 - sonnet.utilization))
    if model_bits:
        parts.append(", ".join(model_bits))

    return ". ".join(parts)


def speak_hourly_status(usage: UsageData, burn_rate: BurnRate) -> None:
    """fire-and-forget: hourly status with remaining and projected utilization.

    format: remaining % first, reset time, then projected window utilization.
    uses kokoro at 1.4x.
    """
    windows_by_name = {w.name: w for w in usage.windows}
    five_hour = windows_by_name.get("five_hour")
    if not five_hour:
        log.warning("no five_hour window available for hourly readout")
        return

    hr_left = int(100 - five_hour.utilization)
    reset = _format_relative_time(five_hour.resets_at)

    parts = ["%d percent remaining" % hr_left]
    if reset:
        parts.append("resets %s" % reset)

    projected = burn_rate.projected_remaining_at_reset
    if projected is not None:
        projected_used = max(0, min(100, 100 - int(projected)))
        parts.append("on pace to use %d percent of the window" % projected_used)

    text = ". ".join(parts)
    log.info("hourly readout: %(text)s", {"text": text})
    _speak_kokoro(text)


def speak_full_status(usage: UsageData) -> None:
    """fire-and-forget: thorough voice status via kokoro at 1.4x speed.

    covers hourly, weekly, per-model breakdown, and reset times.
    uses kokoro mode for speed control.
    """
    text = format_voice_status(usage)
    log.info("voice readout: %(text)s", {"text": text})
    _speak_kokoro(text)


def _speak_kokoro(text: str) -> None:
    """shared helper: fire-and-forget kokoro speech at 1.4x speed.

    waits for sttts mic to go idle before speaking, so announcements
    don't bleed into voice recordings.
    """
    _wait_for_mic_idle()
    try:
        subprocess.Popen(
            ["cute-say", "-k", "-s", "1.4", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        log.warning("cute-say not found in PATH, skipping speech")


def announce_auth_expired() -> None:
    """tell the user their auth token expired and how to fix it."""
    text = "hey, gotta login"
    log.info("auth expired announcement")
    _speak_kokoro(text)


def announce_auth_login_started() -> None:
    """confirm that the login flow has been kicked off."""
    text = "opening login now"
    log.info("auth login started announcement")
    _speak_kokoro(text)


def announce_auth_login_result(success: bool) -> None:
    """report whether re-authentication succeeded or failed."""
    if success:
        text = "logged back in, resuming"
    else:
        text = "that didn't work, try again"
    log.info(
        "auth login result: %(result)s",
        {"result": "success" if success else "failure"},
    )
    _speak_kokoro(text)
