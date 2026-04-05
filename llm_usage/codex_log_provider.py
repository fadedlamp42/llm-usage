"""codex usage provider backed by local codex session logs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from prism.logging import get_logger

from llm_usage.providers import UsageProvider
from llm_usage.usage import UsageData, UsageWindow

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger()

FIVE_HOUR_WINDOW_MINUTES = 300
SEVEN_DAY_WINDOW_MINUTES = 7 * 24 * 60


def _window_name_for_minutes(minutes: int) -> str:
    if minutes == FIVE_HOUR_WINDOW_MINUTES:
        return "five_hour"
    if minutes == SEVEN_DAY_WINDOW_MINUTES:
        return "seven_day"
    if minutes % (24 * 60) == 0:
        return "window_%dd" % (minutes // (24 * 60))
    if minutes % 60 == 0:
        return "window_%dh" % (minutes // 60)
    return "window_%dm" % minutes


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


class CodexLogUsageProvider(UsageProvider):
    """usage provider backed by local codex session jsonl logs."""

    provider_name = "codex_logs"

    def __init__(self, sessions_root: Path, max_staleness_seconds: int):
        self.sessions_root = sessions_root.expanduser()
        self.max_staleness_seconds = max_staleness_seconds

        self._active_session_file: Path | None = None
        self._active_read_offset = 0

        self._latest_windows: list[UsageWindow] = []
        self._latest_event_time: datetime | None = None

    def fetch_usage(self) -> UsageData:
        self._refresh_usage_windows()

        if not self._latest_windows:
            raise RuntimeError(
                "no codex usage windows found yet. open codex and run at least one turn or `/status`, "
                "then retry"
            )

        if self._latest_event_time is not None and self.max_staleness_seconds > 0:
            age_seconds = (datetime.now(UTC) - self._latest_event_time).total_seconds()
            if age_seconds > self.max_staleness_seconds:
                logger.warning(
                    "codex usage data is stale",
                    age_seconds=round(age_seconds),
                    max_staleness_seconds=self.max_staleness_seconds,
                )

        windows = list(self._latest_windows)
        most_constrained = max(windows, key=lambda window: window.utilization)
        return UsageData(windows=windows, most_constrained=most_constrained)

    def _refresh_usage_windows(self) -> None:
        latest_file = self._find_latest_session_file()
        if latest_file is None:
            return

        if latest_file != self._active_session_file:
            self._active_session_file = latest_file
            self._active_read_offset = 0

        if self._active_session_file is None:
            return

        try:
            file_size = self._active_session_file.stat().st_size
        except OSError:
            return

        if file_size < self._active_read_offset:
            self._active_read_offset = 0

        with self._active_session_file.open(encoding="utf-8") as handle:
            handle.seek(self._active_read_offset)
            for raw_line in handle:
                parsed = self._parse_token_count_event(raw_line)
                if parsed is None:
                    continue

                windows, event_time = parsed
                self._latest_windows = windows
                if event_time is not None:
                    self._latest_event_time = event_time

            self._active_read_offset = handle.tell()

    def _find_latest_session_file(self) -> Path | None:
        if not self.sessions_root.exists():
            return None

        latest_file: Path | None = None
        latest_mtime = -1.0
        for candidate in self.sessions_root.rglob("*.jsonl"):
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue

            if mtime > latest_mtime:
                latest_file = candidate
                latest_mtime = mtime

        return latest_file

    def _parse_token_count_event(
        self,
        raw_line: str,
    ) -> tuple[list[UsageWindow], datetime | None] | None:
        raw_line = raw_line.strip()
        if not raw_line:
            return None

        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return None

        if event.get("type") != "event_msg":
            return None

        payload = event.get("payload") or {}
        if payload.get("type") != "token_count":
            return None

        rate_limits = payload.get("rate_limits") or {}
        windows = self._windows_from_rate_limits(rate_limits)
        if not windows:
            return None

        event_time = None
        timestamp_text = event.get("timestamp")
        if timestamp_text:
            try:
                event_time = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
            except ValueError:
                event_time = None

        return windows, event_time

    def _windows_from_rate_limits(self, rate_limits: dict) -> list[UsageWindow]:
        windows: list[UsageWindow] = []
        used_names: set[str] = set()

        for key, value in rate_limits.items():
            if not isinstance(value, dict):
                continue

            used_percent = value.get("used_percent")
            window_minutes = value.get("window_minutes")
            if used_percent is None:
                continue
            if window_minutes is None:
                continue

            try:
                utilization = float(used_percent)
                minutes = int(window_minutes)
            except (TypeError, ValueError):
                continue

            window_name = _window_name_for_minutes(minutes)
            if window_name in used_names:
                window_name = "%s_%s" % (window_name, key)

            windows.append(
                UsageWindow(
                    name=window_name,
                    utilization=utilization,
                    resets_at=_parse_reset_timestamp(value.get("resets_at")),
                )
            )
            used_names.add(window_name)

        windows.sort(key=lambda window: window.name)
        return windows
