"""usage provider adapters for brightness-monitor.

supports:
  - claude: polls anthropic oauth usage endpoint (existing behavior)
  - codex: polls codex/chatgpt usage endpoint (`/backend-api/wham/usage`)
  - codex_logs: reads local codex session logs for rate-limit windows
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from prism.logging import get_logger

from brightness_monitor.auth import attempt_reauth
from brightness_monitor.usage import (
    ProfileInfo,
    UsageData,
    fetch_profile,
    fetch_usage,
    get_token,
    token_fingerprint,
)

if TYPE_CHECKING:
    from brightness_monitor.config import Config

logger = get_logger()


class UsageProvider:
    """provider contract for all usage backends."""

    provider_name = "unknown"

    def fetch_usage(self) -> UsageData:
        raise NotImplementedError

    def attempt_reauth(self) -> bool:
        return False


class ClaudeUsageProvider(UsageProvider):
    """usage provider that delegates to the existing claude oauth implementation.

    caches account profile info and invalidates it when the OAuth token
    changes (i.e. when the user switches accounts), so account switches
    are detected reactively without requiring a daemon restart.
    """

    provider_name = "claude"

    def __init__(self, token_override: str | None = None):
        self._token_override = token_override
        self._cached_profile: ProfileInfo | None = None
        self._last_token_fingerprint: str | None = None

    def fetch_usage(self) -> UsageData:
        token = get_token(explicit_token=self._token_override)

        # detect account switch by comparing token fingerprints
        fingerprint = token_fingerprint(token)
        if fingerprint != self._last_token_fingerprint:
            try:
                self._cached_profile = fetch_profile(token)
                logger.info(
                    "resolved account profile",
                    email=self._cached_profile.email,
                    plan=self._cached_profile.organization_type,
                    tier=self._cached_profile.rate_limit_tier,
                )
            except Exception as error:
                # profile fetch is best-effort; usage polling should not break
                logger.warning("failed to fetch account profile", error=str(error))
                self._cached_profile = None
            self._last_token_fingerprint = fingerprint

        usage = fetch_usage(token)
        usage.account_email = self._cached_profile.email if self._cached_profile else None
        return usage

    def attempt_reauth(self) -> bool:
        # invalidate profile cache so it gets re-fetched with the new token
        self._last_token_fingerprint = None
        return attempt_reauth()


def create_usage_provider(config: Config, token_override: str | None = None) -> UsageProvider:
    """build the configured usage provider instance."""
    provider_name = config.provider.name.strip().lower()

    if provider_name == "claude":
        return ClaudeUsageProvider(token_override=token_override)

    if provider_name in {"codex", "codex_api"}:
        from brightness_monitor.codex_api_provider import CodexApiUsageProvider

        codex_config = config.provider.codex
        return CodexApiUsageProvider(
            auth_file=Path(codex_config.auth_file),
            fallback_auth_files=codex_config.fallback_auth_files,
            usage_url=codex_config.usage_url,
            refresh_url=codex_config.refresh_url,
            refresh_client_id=codex_config.refresh_client_id,
            request_timeout_seconds=codex_config.request_timeout_seconds,
            token_override=token_override,
        )

    if provider_name == "codex_logs":
        from brightness_monitor.codex_log_provider import CodexLogUsageProvider

        if token_override:
            logger.warning("ignoring --token for codex_logs provider")

        codex_config = config.provider.codex
        return CodexLogUsageProvider(
            sessions_root=Path(codex_config.sessions_root),
            max_staleness_seconds=codex_config.max_staleness_seconds,
        )

    raise RuntimeError(
        "unknown provider '%(provider)s'. expected one of: claude, codex, codex_logs"
        % {"provider": config.provider.name}
    )
