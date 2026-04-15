"""voice output for llm-usage via prism.mac.speech.

three modes:
  - hourly status: remaining %, reset time, and pace observation via naturalized chatterbox
  - full report: thorough status via kokoro at 1.4x speed covering all windows
  - auth alerts: notify about expired tokens and prompt for re-login

mic coordination (waiting for sttts mic idle before speaking) is handled
by prism.mac.speech — just pass the relay URL through configure().
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from prism.logging import get_logger
from prism.mac.speech import configure as _configure_speech
from prism.mac.speech import say as _prism_say

if TYPE_CHECKING:
    from llm_usage.storage import AccountUtilization, BurnRate
    from llm_usage.usage import UsageData

logger = get_logger()


def configure(sttts_relay_url: str | None = None) -> None:
    """configure speech, passing mic coordination through to prism.mac.speech."""
    _configure_speech(sttts_relay_url=sttts_relay_url)


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


def _format_minutes_until_limit(minutes: float) -> str:
    """format estimated minutes until 100% utilization as natural speech.

    produces phrases like "hitting the limit in 47 minutes",
    "hitting the limit in about an hour", "hitting the limit any moment".
    """
    if minutes < 1:
        return "hitting the limit any moment"
    if minutes < 2:
        return "hitting the limit in about a minute"
    if minutes < 60:
        return "hitting the limit in %d minutes" % int(minutes)
    if minutes < 90:
        return "hitting the limit in about an hour"
    return "hitting the limit in about %d hours" % round(minutes / 60)


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


WEEKLY_WARNING_THRESHOLD = 90.0


def speak_hourly_status(usage: UsageData, burn_rate: BurnRate) -> None:
    """fire-and-forget: hourly status with remaining and projected utilization.

    format: remaining % first, reset time, then projected utilization
    personalized with the account name. appends a weekly warning
    when the seven_day window is at or above 90%.
    """
    windows_by_name = {w.name: w for w in usage.windows}
    five_hour = windows_by_name.get("five_hour")
    if not five_hour:
        logger.warning("no five_hour window available for hourly readout")
        return

    hr_left = int(100 - five_hour.utilization)
    reset = _format_relative_time(five_hour.resets_at)
    possessive = _email_to_possessive_name(usage.account_email)

    parts = ["%d percent remaining" % hr_left]
    if reset:
        parts.append("resets %s" % reset)

    projected = burn_rate.projected_remaining_at_reset
    if projected is not None:
        will_exceed = projected < 0
        if will_exceed and burn_rate.minutes_until_limit is not None:
            # on track to blow past the limit — report time-to-wall
            parts.append(_format_minutes_until_limit(burn_rate.minutes_until_limit))
        else:
            # comfortable pace — report projected utilization at reset
            projected_used = max(0, min(100, 100 - int(projected)))
            if possessive:
                parts.append(
                    "to use %(used)d percent of %(name)s window"
                    % {"used": projected_used, "name": possessive}
                )
            else:
                parts.append("to use %d percent of the window" % projected_used)

    # warn when the weekly window is getting tight
    seven_day = windows_by_name.get("seven_day")
    if seven_day and seven_day.utilization >= WEEKLY_WARNING_THRESHOLD:
        weekly_used = int(seven_day.utilization)
        parts.append("weekly at %d too" % weekly_used)

    text = ". ".join(parts)
    logger.info("hourly readout", text=text)
    _speak_kokoro(text)


def speak_full_status(usage: UsageData) -> None:
    """fire-and-forget: thorough voice status via kokoro at 1.4x speed.

    covers hourly, weekly, per-model breakdown, and reset times.
    uses kokoro mode for speed control.
    """
    text = format_voice_status(usage)
    logger.info("voice readout", text=text)
    _speak_kokoro(text)


def _speak_kokoro(text: str) -> None:
    """shared helper: fire-and-forget kokoro speech at 1.4x speed.

    mic coordination (waiting for idle) is handled by prism.mac.speech.say().
    """
    _prism_say(text, blocking=False, extra_args=["-k", "-s", "1.4"])


def _email_to_possessive_name(email: str | None) -> str:
    """short possessive form of an account email for natural speech.

    uses just the local part since it's always in-context (no ambiguity).
    "fadedlamp@gmail.com" -> "fadedlamp's"
    "peter@prism-dynamics.org" -> "peter's"
    """
    if not email:
        return ""
    local = email.split("@", 1)[0]
    return "%(local)s's" % {"local": local}


def _email_to_spoken_name(email: str) -> str:
    """turn an email into something natural to say aloud.

    "fadedlamp@gmail.com" -> "fadedlamp"
    "peter@prism-dynamics.org" -> "peter at prism dynamics"
    """
    local, domain = email.split("@", 1)
    # for common providers, just use the local part
    if domain in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"):
        return local
    # otherwise include domain spoken naturally
    domain_name = domain.split(".")[0].replace("-", " ")
    return "%(local)s at %(domain)s" % {"local": local, "domain": domain_name}


def suggest_account_switch(
    alternatives: list[AccountUtilization],
    unknown_emails: list[str],
) -> None:
    """suggest switching to an alternative account via speech.

    if we have utilization data for alternatives, recommend the one
    with the most remaining capacity. otherwise just list the options.
    """
    # prefer accounts we have data on (sorted by remaining, best first)
    if alternatives:
        best = alternatives[0]
        name = _email_to_spoken_name(best.account_email)
        remaining = int(best.remaining)
        text = "running low. %(name)s has %(remaining)d percent left" % {
            "name": name,
            "remaining": remaining,
        }
    elif unknown_emails:
        names = [_email_to_spoken_name(email) for email in unknown_emails]
        if len(names) == 1:
            text = "running low. maybe switch to %s" % names[0]
        else:
            text = "running low. maybe switch to %s or %s" % (
                ", ".join(names[:-1]),
                names[-1],
            )
    else:
        return  # no alternatives to suggest

    logger.info("account switch suggestion", text=text)
    _speak_kokoro(text)


def announce_auth_expired() -> None:
    """tell the user their auth token expired and how to fix it."""
    text = "hey, gotta login"
    logger.info("auth expired announcement")
    _speak_kokoro(text)


def announce_auth_login_started() -> None:
    """confirm that the login flow has been kicked off."""
    text = "opening login now"
    logger.info("auth login started announcement")
    _speak_kokoro(text)


def announce_auth_login_result(success: bool) -> None:
    """report whether re-authentication succeeded or failed."""
    if success:
        text = "logged back in, resuming"
    else:
        text = "that didn't work, try again"
    logger.info("auth login result", success=success)
    _speak_kokoro(text)
