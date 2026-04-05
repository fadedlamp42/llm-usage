"""Claude API usage polling via the undocumented OAuth usage endpoint.

retrieves the same data visible at /limits in Claude Code:
five-hour window utilization, seven-day windows, and reset times.

also provides account profile resolution via /api/oauth/profile,
with token-fingerprint-based caching so account switches are detected
reactively without requiring a daemon restart.

credential resolution order:
  1. --token CLI flag (passed through to get_token)
  2. CLAUDE_OAUTH_TOKEN environment variable
  3. macOS Keychain ("Claude Code-credentials" service)
"""

from __future__ import annotations

import hashlib
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
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
KEYCHAIN_SERVICE = "Claude Code-credentials"
TOKEN_ENV_VAR = "CLAUDE_OAUTH_TOKEN"


class AuthExpiredError(RuntimeError):
    """raised when the OAuth token is expired or invalid (HTTP 401)."""


@dataclass
class ProfileInfo:
    """account and organization metadata from the profile endpoint."""

    email: str
    full_name: str
    account_uuid: str
    organization_name: str
    organization_type: str  # e.g. "claude_max"
    rate_limit_tier: str  # e.g. "default_claude_max_20x"
    subscription_status: str  # e.g. "active"
    has_extra_usage_enabled: bool


@dataclass
class ExtraUsage:
    """overage/extra usage billing state from the usage response."""

    is_enabled: bool
    monthly_limit: float | None
    used_credits: float | None
    utilization: float | None


@dataclass
class UsageWindow:
    name: str
    utilization: float  # 0-100, percentage of window consumed
    resets_at: datetime | None


@dataclass
class UsageData:
    windows: list[UsageWindow]
    most_constrained: UsageWindow
    extra_usage: ExtraUsage | None = None
    account_email: str | None = None


def _token_from_keychain() -> str | None:
    """try to pull the OAuth token from macOS Keychain.

    # delegates to prism.mac.keychain.read_json

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


def _make_api_request(url: str, token: str) -> dict:
    """make an authenticated GET request to an anthropic OAuth endpoint."""
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer %(token)s" % {"token": token},
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "llm-usage/0.1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise AuthExpiredError(
                "OAuth token expired or invalid; re-authenticate or provide a fresh token"
            ) from error
        raise RuntimeError(
            "API request failed: HTTP %(code)s (%(url)s)" % {"code": error.code, "url": url}
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            "network error: %(error)s (%(url)s)" % {"error": error, "url": url}
        ) from error


def _is_usage_window(value: object) -> bool:
    """check whether a response value looks like a rate-limit window.

    windows are dicts with a `utilization` key and no `is_enabled` key
    (which distinguishes them from the extra_usage object).
    """
    if not isinstance(value, dict):
        return False
    if "utilization" not in value:
        return False
    # extra_usage has is_enabled; regular windows don't
    return "is_enabled" not in value


def _parse_extra_usage(data: dict) -> ExtraUsage | None:
    """parse the extra_usage object from the usage response, if present."""
    raw = data.get("extra_usage")
    if raw is None or not isinstance(raw, dict):
        return None

    return ExtraUsage(
        is_enabled=raw.get("is_enabled", False),
        monthly_limit=raw.get("monthly_limit"),
        used_credits=raw.get("used_credits"),
        utilization=raw.get("utilization"),
    )


def fetch_profile(token: str) -> ProfileInfo:
    """fetch account profile from the anthropic OAuth profile endpoint."""
    data = _make_api_request(PROFILE_URL, token)

    account = data.get("account", {})
    organization = data.get("organization", {})

    return ProfileInfo(
        email=account.get("email", ""),
        full_name=account.get("full_name", ""),
        account_uuid=account.get("uuid", ""),
        organization_name=organization.get("name", ""),
        organization_type=organization.get("organization_type", ""),
        rate_limit_tier=organization.get("rate_limit_tier", ""),
        subscription_status=organization.get("subscription_status", ""),
        has_extra_usage_enabled=organization.get("has_extra_usage_enabled", False),
    )


def token_fingerprint(token: str) -> str:
    """short hash of a token for change detection without storing the raw value."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def fetch_usage(token: str) -> UsageData:
    """fetch current usage from Claude's OAuth usage endpoint.

    dynamically discovers all rate-limit windows in the response
    rather than relying on a hardcoded list, so new windows
    anthropic adds are captured automatically.
    """
    data = _make_api_request(USAGE_URL, token)

    # dynamically discover all window-like entries in the response
    windows: list[UsageWindow] = []

    for key, value in data.items():
        if not _is_usage_window(value):
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
    extra_usage = _parse_extra_usage(data)

    return UsageData(
        windows=windows,
        most_constrained=most_constrained,
        extra_usage=extra_usage,
    )
