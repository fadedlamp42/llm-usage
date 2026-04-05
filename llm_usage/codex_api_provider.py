"""codex usage provider backed by codex/chatgpt usage API."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from prism.logging import get_logger

from llm_usage.providers import UsageProvider
from llm_usage.usage import AuthExpiredError, UsageData, UsageWindow

logger = get_logger()

FIVE_HOUR_WINDOW_SECONDS = 5 * 60 * 60
SEVEN_DAY_WINDOW_SECONDS = 7 * 24 * 60 * 60

TOKEN_EXPIRY_LEEWAY_SECONDS = 30
OAUTH_GRANT_TYPE = "refresh_token"
KNOWN_REFRESH_TOKEN_ERRORS = {
    "refresh_token_expired",
    "refresh_token_reused",
    "refresh_token_invalidated",
}


def _window_name_for_seconds(seconds: int) -> str:
    if seconds == FIVE_HOUR_WINDOW_SECONDS:
        return "five_hour"
    if seconds == SEVEN_DAY_WINDOW_SECONDS:
        return "seven_day"

    rounded_minutes = (seconds + 59) // 60
    if rounded_minutes % (24 * 60) == 0:
        return "window_%dd" % (rounded_minutes // (24 * 60))
    if rounded_minutes % 60 == 0:
        return "window_%dh" % (rounded_minutes // 60)
    return "window_%dm" % rounded_minutes


def _parse_reset_timestamp(value) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    return None


def _safe_string(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _extract_error_code(payload: dict | None) -> str | None:
    if payload is None:
        return None

    error_value = payload.get("error")
    if isinstance(error_value, dict):
        return _safe_string(error_value.get("code"))

    if isinstance(error_value, str):
        return _safe_string(error_value)

    return _safe_string(payload.get("code"))


def _slug_name(value: str) -> str:
    pieces = []
    for character in value.lower():
        if character.isalnum():
            pieces.append(character)
            continue
        pieces.append("_")

    slug = "".join(pieces).strip("_")
    if not slug:
        return "extra"

    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug


class CodexApiUsageProvider(UsageProvider):
    """usage provider that polls codex/chatgpt usage endpoint."""

    provider_name = "codex"

    def __init__(
        self,
        auth_file: Path,
        fallback_auth_files: list[str],
        usage_url: str,
        refresh_url: str,
        refresh_client_id: str,
        request_timeout_seconds: int,
        token_override: str | None = None,
    ):
        self.auth_file = auth_file.expanduser()
        self.fallback_auth_files = [Path(path).expanduser() for path in fallback_auth_files]

        self.usage_url = usage_url
        self.refresh_url = refresh_url
        self.refresh_client_id = refresh_client_id
        self.request_timeout_seconds = max(1, request_timeout_seconds)

        self._token_override = token_override
        self._access_token = token_override
        self._refresh_token: str | None = None
        self._account_id: str | None = None

    def fetch_usage(self) -> UsageData:
        self._ensure_auth_loaded()

        try:
            payload = self._request_usage_payload()
        except AuthExpiredError as initial_error:
            self._refresh_access_token()
            try:
                payload = self._request_usage_payload()
            except AuthExpiredError as retry_error:
                raise retry_error from initial_error

        windows = self._windows_from_usage_payload(payload)
        if not windows:
            raise RuntimeError("no codex usage windows returned from usage endpoint")

        most_constrained = max(windows, key=lambda window: window.utilization)
        return UsageData(windows=windows, most_constrained=most_constrained)

    def attempt_reauth(self) -> bool:
        if self._token_override:
            logger.warning("cannot re-authenticate with explicit --token override")
            return False

        commands: list[list[str]] = []

        codex_path = shutil.which("codex")
        if codex_path:
            commands.append([codex_path, "login"])

        opencode_path = shutil.which("opencode")
        if opencode_path:
            commands.append([opencode_path, "auth", "login", "openai"])

        if not commands:
            logger.warning("no codex-compatible CLI found for re-authentication")
            return False

        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    timeout=180,
                    capture_output=True,
                    text=True,
                )
            except Exception as error:
                logger.warning(
                    "reauth command failed to run",
                    command=" ".join(command),
                    error=str(error),
                )
                continue

            if result.returncode == 0:
                self._access_token = None
                self._refresh_token = None
                self._account_id = None
                logger.info("reauth command succeeded", command=" ".join(command))
                return True

            logger.warning(
                "reauth command failed",
                command=" ".join(command),
                exit_code=result.returncode,
                stderr=result.stderr.strip(),
            )

        return False

    def _ensure_auth_loaded(self) -> None:
        if self._access_token:
            return

        if self._token_override:
            self._access_token = self._token_override
            return

        self._load_tokens_from_auth_files()

    def _candidate_auth_files(self) -> list[Path]:
        ordered_paths = [self.auth_file, *self.fallback_auth_files]

        deduplicated: list[Path] = []
        seen_paths: set[str] = set()
        for candidate in ordered_paths:
            normalized = str(candidate)
            if normalized in seen_paths:
                continue
            seen_paths.add(normalized)
            deduplicated.append(candidate)

        return deduplicated

    def _load_tokens_from_auth_files(self) -> None:
        now_epoch = int(time.time())
        token_candidates: list[tuple[Path, str, str | None, str | None, int | None]] = []

        for auth_path in self._candidate_auth_files():
            if not auth_path.exists():
                continue

            try:
                payload = json.loads(auth_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            parsed_tokens = self._extract_tokens_from_auth_payload(payload)
            if parsed_tokens is None:
                continue

            access_token, refresh_token, account_id = parsed_tokens
            expiration_epoch = self._token_expiration_epoch(access_token)
            token_candidates.append(
                (auth_path, access_token, refresh_token, account_id, expiration_epoch)
            )

        if not token_candidates:
            searched = [str(path) for path in self._candidate_auth_files()]
            raise RuntimeError(
                "no codex auth token found. run `codex login` (or `opencode auth login openai`) "
                "or set provider.codex.auth_file to a valid auth file. searched: %(paths)s"
                % {"paths": searched}
            )

        def candidate_key(
            candidate: tuple[Path, str, str | None, str | None, int | None],
        ) -> tuple[int, int]:
            _, _, _, _, expiration_epoch = candidate

            if expiration_epoch is not None:
                expired = expiration_epoch <= now_epoch + TOKEN_EXPIRY_LEEWAY_SECONDS
                if expired:
                    return (0, expiration_epoch)
                return (1, expiration_epoch)

            return (1, now_epoch)

        chosen = max(token_candidates, key=candidate_key)
        auth_path, access_token, refresh_token, account_id, _ = chosen

        self._access_token = access_token
        self._refresh_token = refresh_token
        self._account_id = account_id or self._account_id_from_access_token(access_token)

        logger.debug("loaded codex auth token", auth_file=str(auth_path))

    def _extract_tokens_from_auth_payload(
        self,
        payload: dict,
    ) -> tuple[str, str | None, str | None] | None:
        codex_tokens = payload.get("tokens")
        if isinstance(codex_tokens, dict):
            access_token = _safe_string(codex_tokens.get("access_token"))
            if access_token:
                refresh_token = _safe_string(codex_tokens.get("refresh_token"))
                account_id = _safe_string(codex_tokens.get("account_id"))
                return access_token, refresh_token, account_id

        opencode_openai = payload.get("openai")
        if isinstance(opencode_openai, dict):
            access_token = _safe_string(opencode_openai.get("access"))
            if access_token:
                refresh_token = _safe_string(opencode_openai.get("refresh"))
                account_id = _safe_string(opencode_openai.get("accountId"))
                return access_token, refresh_token, account_id

        return None

    def _request_usage_payload(self) -> dict:
        if self._access_token is None:
            raise RuntimeError("no codex access token available")

        headers = {
            "Authorization": "Bearer %(token)s" % {"token": self._access_token},
            "User-Agent": "llm-usage/0.1.0",
        }
        if self._account_id:
            headers["ChatGPT-Account-Id"] = self._account_id

        request = urllib.request.Request(self.usage_url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
                payload = json.loads(response.read().decode())
                if isinstance(payload, dict):
                    return payload
                raise RuntimeError("unexpected codex usage response shape")
        except urllib.error.HTTPError as error:
            body_text = error.read().decode(errors="replace")
            payload = self._try_parse_json(body_text)
            if error.code == 401:
                error_code = _extract_error_code(payload)
                if error_code:
                    raise AuthExpiredError(
                        "codex auth expired or invalid (%(code)s)" % {"code": error_code}
                    ) from error
                raise AuthExpiredError("codex auth expired or invalid") from error

            raise RuntimeError(
                "codex usage request failed: HTTP %(code)s: %(body)s"
                % {"code": error.code, "body": body_text}
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(
                "network error fetching codex usage: %(error)s" % {"error": error}
            ) from error

    def _refresh_access_token(self) -> None:
        if self._token_override:
            raise AuthExpiredError("explicit --token override expired; provide a fresh token")

        if not self._refresh_token:
            raise AuthExpiredError(
                "codex refresh token unavailable; run `codex login` or `opencode auth login openai`"
            )

        refresh_payload = json.dumps(
            {
                "client_id": self.refresh_client_id,
                "grant_type": OAUTH_GRANT_TYPE,
                "refresh_token": self._refresh_token,
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            self.refresh_url,
            data=refresh_payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "llm-usage/0.1.0",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            body_text = error.read().decode(errors="replace")
            payload = self._try_parse_json(body_text)

            if error.code == 401:
                error_code = _extract_error_code(payload)
                if error_code in KNOWN_REFRESH_TOKEN_ERRORS:
                    raise AuthExpiredError(
                        "codex refresh token expired or revoked (%(code)s)" % {"code": error_code}
                    ) from error

                raise AuthExpiredError("codex refresh token rejected") from error

            raise RuntimeError(
                "codex token refresh failed: HTTP %(code)s: %(body)s"
                % {"code": error.code, "body": body_text}
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(
                "network error refreshing codex token: %(error)s" % {"error": error}
            ) from error

        if not isinstance(payload, dict):
            raise RuntimeError("codex token refresh returned unexpected payload")

        access_token = _safe_string(payload.get("access_token"))
        if not access_token:
            raise RuntimeError("codex token refresh returned no access_token")

        self._access_token = access_token

        refreshed_refresh_token = _safe_string(payload.get("refresh_token"))
        if refreshed_refresh_token:
            self._refresh_token = refreshed_refresh_token

        if not self._account_id:
            self._account_id = self._account_id_from_access_token(access_token)

        logger.info("refreshed codex access token")

    def _windows_from_usage_payload(self, payload: dict) -> list[UsageWindow]:
        windows: list[UsageWindow] = []

        windows.extend(self._windows_from_rate_limit_block(payload.get("rate_limit"), suffix=None))
        windows.extend(
            self._windows_from_rate_limit_block(
                payload.get("code_review_rate_limit"),
                suffix="code_review",
            )
        )

        additional_rate_limits = payload.get("additional_rate_limits")
        if isinstance(additional_rate_limits, list):
            for additional in additional_rate_limits:
                if not isinstance(additional, dict):
                    continue

                limit_suffix = _safe_string(additional.get("metered_feature"))
                if limit_suffix is None:
                    limit_suffix = _safe_string(additional.get("limit_name"))

                windows.extend(
                    self._windows_from_rate_limit_block(
                        additional.get("rate_limit"),
                        suffix=limit_suffix,
                    )
                )

        unique_windows: list[UsageWindow] = []
        used_names: set[str] = set()
        for window in windows:
            if window.name in used_names:
                continue
            used_names.add(window.name)
            unique_windows.append(window)

        unique_windows.sort(key=lambda window: window.name)
        return unique_windows

    def _windows_from_rate_limit_block(self, value, suffix: str | None) -> list[UsageWindow]:
        if not isinstance(value, dict):
            return []

        windows: list[UsageWindow] = []

        primary = self._window_from_snapshot(value.get("primary_window"), suffix)
        if primary is not None:
            windows.append(primary)

        secondary = self._window_from_snapshot(value.get("secondary_window"), suffix)
        if secondary is not None:
            windows.append(secondary)

        return windows

    def _window_from_snapshot(self, value, suffix: str | None) -> UsageWindow | None:
        if not isinstance(value, dict):
            return None

        used_percent = value.get("used_percent")
        limit_window_seconds = value.get("limit_window_seconds")

        if used_percent is None:
            return None
        if limit_window_seconds is None:
            return None

        try:
            utilization = float(used_percent)
            seconds = int(limit_window_seconds)
        except (TypeError, ValueError):
            return None

        window_name = _window_name_for_seconds(seconds)
        if suffix:
            window_name = "%(name)s_%(suffix)s" % {
                "name": window_name,
                "suffix": _slug_name(suffix),
            }

        return UsageWindow(
            name=window_name,
            utilization=utilization,
            resets_at=_parse_reset_timestamp(value.get("reset_at")),
        )

    def _token_expiration_epoch(self, token: str) -> int | None:
        jwt_payload = self._jwt_payload(token)
        if jwt_payload is None:
            return None

        expiration = jwt_payload.get("exp")
        if isinstance(expiration, (int, float)):
            return int(expiration)
        return None

    def _account_id_from_access_token(self, token: str) -> str | None:
        jwt_payload = self._jwt_payload(token)
        if jwt_payload is None:
            return None

        auth_claims = jwt_payload.get("https://api.openai.com/auth")
        if isinstance(auth_claims, dict):
            account_id = _safe_string(auth_claims.get("chatgpt_account_id"))
            if account_id:
                return account_id

            user_id = _safe_string(auth_claims.get("chatgpt_user_id"))
            if user_id:
                return user_id

        return _safe_string(jwt_payload.get("sub"))

    def _jwt_payload(self, token: str) -> dict | None:
        token_parts = token.split(".")
        if len(token_parts) < 2:
            return None

        payload_b64 = token_parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        try:
            payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
            payload = json.loads(payload_bytes.decode())
        except (ValueError, json.JSONDecodeError):
            return None

        if isinstance(payload, dict):
            return payload
        return None

    def _try_parse_json(self, body_text: str) -> dict | None:
        try:
            payload = json.loads(body_text)
        except json.JSONDecodeError:
            return None

        if isinstance(payload, dict):
            return payload
        return None
