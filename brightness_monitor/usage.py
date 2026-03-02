"""Claude API usage polling via the undocumented OAuth usage endpoint.

retrieves the same data visible at /limits in Claude Code:
five-hour window utilization, seven-day windows, and reset times.

credential resolution order:
  1. --token CLI flag (passed through to get_token)
  2. CLAUDE_OAUTH_TOKEN environment variable
  3. macOS Keychain ("Claude Code-credentials" service)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from prism.logging import get_logger
from prism.mac.keychain import read_json as _read_keychain_json

logger = get_logger()

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
TOKEN_ENV_VAR = "CLAUDE_OAUTH_TOKEN"


class AuthExpiredError(RuntimeError):
    """raised when the OAuth token is expired or invalid (HTTP 401)."""


@dataclass
class UsageWindow:
    name: str
    utilization: float  # 0-100, percentage of window consumed
    resets_at: datetime | None


@dataclass
class UsageData:
    windows: list[UsageWindow]
    most_constrained: UsageWindow


def _token_from_keychain() -> str | None:
    """try to pull the OAuth token from macOS Keychain.

    # TODO: swap brightness-monitor to use prism.mac.keychain
    # (swap complete — now delegates to prism.mac.keychain.read_json)

    Claude Code stores credentials under the service name
    "Claude Code-credentials" as a JSON blob containing
    claudeAiOauth.accessToken.
    """
    credentials = _read_keychain_json(KEYCHAIN_SERVICE)
    if credentials is None:
        return None
    try:
        token = credentials["claudeAiOauth"]["accessToken"]
        logger.debug("got OAuth token from keychain")
        return token
    except KeyError:
        return None


def _token_from_env() -> str | None:
    """check for CLAUDE_OAUTH_TOKEN environment variable."""
    token = os.environ.get(TOKEN_ENV_VAR)
    if token:
        logger.debug("got OAuth token from env", var=TOKEN_ENV_VAR)
    return token


def get_token(explicit_token: str | None = None) -> str:
    """resolve an OAuth token from all available sources.

    tries in order: explicit value, env var, Keychain.
    raises RuntimeError if none found.
    """
    if explicit_token:
        logger.debug("using explicitly provided token")
        return explicit_token

    token = _token_from_env()
    if token:
        return token

    token = _token_from_keychain()
    if token:
        return token

    raise RuntimeError(
        "no Claude OAuth token found. provide one via:\n"
        "  1. --token flag\n"
        "  2. %(env)s environment variable\n"
        "  3. macOS Keychain (auto-populated by Claude Code OAuth login)" % {"env": TOKEN_ENV_VAR}
    )


def fetch_usage(token: str) -> UsageData:
    """fetch current usage from Claude's OAuth usage endpoint.

    returns utilization percentages for each rate-limit window
    and identifies the most constrained one.
    """
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": "Bearer %(token)s" % {"token": token},
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "brightness-monitor/0.1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise AuthExpiredError(
                "OAuth token expired or invalid; re-authenticate or provide a fresh token"
            ) from error
        raise
    except urllib.error.URLError as error:
        raise RuntimeError("network error fetching usage: %(error)s" % {"error": error}) from error

    window_keys = (
        "five_hour",
        "seven_day",
        "seven_day_sonnet",
        "seven_day_opus",
        "seven_day_oauth_apps",
        "seven_day_cowork",
    )
    windows: list[UsageWindow] = []

    for key in window_keys:
        value = data.get(key)
        if value is None:
            continue
        if value.get("utilization") is None:
            continue

        resets_at = None
        if value.get("resets_at"):
            resets_at = datetime.fromisoformat(value["resets_at"])

        windows.append(
            UsageWindow(
                name=key,
                utilization=value["utilization"],
                resets_at=resets_at,
            )
        )

    if not windows:
        raise RuntimeError("no usage windows returned from API")

    most_constrained = max(windows, key=lambda w: w.utilization)

    return UsageData(windows=windows, most_constrained=most_constrained)
