"""HTTP status endpoint for external consumers like utop.

runs in a daemon thread alongside the main loop. serves current
usage state by querying usage.db directly on each request — the
daemon writes to the db on every poll, so the data is always fresh.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from prism.logging import get_logger

from llm_usage.storage import (
    DEFAULT_DB_PATH,
    calculate_burn_rate,
)

logger = get_logger()

DEFAULT_STATUS_PORT = 8387

# how many recent utilization values to include per window (for sparklines).
# at 2-minute polls, 30 points = 1 hour of history.
SPARKLINE_HISTORY_COUNT = 30


def _query_status(
    db_path: Path, provider_name: str, tracked_window: str, poll_interval: int
) -> dict:
    """build a status response by querying usage.db."""
    connection = sqlite3.connect(str(db_path), timeout=2)
    try:
        return _build_status_from_db(connection, provider_name, tracked_window, poll_interval)
    finally:
        connection.close()


def _build_status_from_db(
    connection: sqlite3.Connection,
    provider_name: str,
    tracked_window: str,
    poll_interval: int,
) -> dict:
    """query the db and assemble a status dict."""

    # latest poll per window
    rows = connection.execute(
        "SELECT u.window_name, u.utilization, u.resets_at, u.account_email, u.polled_at "
        "FROM usage_polls u "
        "INNER JOIN ("
        "  SELECT window_name, MAX(polled_at) as max_polled "
        "  FROM usage_polls GROUP BY window_name"
        ") latest "
        "ON u.window_name = latest.window_name AND u.polled_at = latest.max_polled"
    ).fetchall()

    if not rows:
        return {"error": "no data in usage.db"}

    # account email from most recent row
    account_email = None
    latest_polled_at = None
    for row in rows:
        polled = row[4]
        if latest_polled_at is None or polled > latest_polled_at:
            latest_polled_at = polled
            account_email = row[3]

    # build window entries with burn rates and sparkline history
    windows = []
    most_constrained_name = None
    most_constrained_util = -1.0

    for window_name, utilization, resets_at_str, _email, _polled in rows:
        # parse reset time for burn rate calculation
        resets_at = None
        if resets_at_str:
            with contextlib.suppress(ValueError):
                resets_at = datetime.fromisoformat(resets_at_str)

        # burn rate from storage module (same logic the daemon uses for speech)
        burn = calculate_burn_rate(
            connection,
            provider_name,
            window_name,
            resets_at,
            account_email=account_email,
        )
        burn_dict = None
        if burn is not None:
            burn_dict = {
                "utilization_per_hour": burn.utilization_per_hour,
                "projected_remaining_at_reset": burn.projected_remaining_at_reset,
                "hours_until_reset": burn.hours_until_reset,
                "sample_minutes": burn.sample_minutes,
                "minutes_until_limit": burn.minutes_until_limit,
            }

        # recent utilization for sparklines
        history_rows = connection.execute(
            "SELECT utilization FROM usage_polls "
            "WHERE window_name = ? "
            "ORDER BY polled_at DESC LIMIT ?",
            (window_name, SPARKLINE_HISTORY_COUNT),
        ).fetchall()
        recent_utilization = [r[0] for r in reversed(history_rows)]

        windows.append(
            {
                "name": window_name,
                "utilization": utilization,
                "resets_at": resets_at_str,
                "burn_rate": burn_dict,
                "recent_utilization": recent_utilization,
            }
        )

        if utilization > most_constrained_util:
            most_constrained_util = utilization
            most_constrained_name = window_name

    # historical account utilization — latest poll per (account, window)
    # from the last 12 hours, so utop can show all accounts at a glance
    account_rows = connection.execute(
        "SELECT u.account_email, u.window_name, u.utilization, u.remaining, u.polled_at "
        "FROM usage_polls u "
        "INNER JOIN ("
        "  SELECT account_email, window_name, MAX(polled_at) as max_polled "
        "  FROM usage_polls "
        "  WHERE account_email IS NOT NULL AND account_email != '' "
        "    AND polled_at > datetime('now', '-12 hours') "
        "  GROUP BY account_email, window_name"
        ") latest "
        "ON u.account_email = latest.account_email "
        "  AND u.window_name = latest.window_name "
        "  AND u.polled_at = latest.max_polled "
        "ORDER BY u.account_email, u.window_name"
    ).fetchall()

    accounts = [
        {
            "account_email": row[0],
            "window_name": row[1],
            "utilization": row[2],
            "remaining": row[3],
            "polled_at": row[4],
        }
        for row in account_rows
    ]

    return {
        "provider": provider_name,
        "account_email": account_email,
        "auth_expired": False,
        "tracked_window": tracked_window,
        "poll_interval": poll_interval,
        "polled_at": latest_polled_at,
        "windows": windows,
        "most_constrained": most_constrained_name,
        "extra_usage": None,
        "accounts": accounts,
    }


# ANSI escapes for SwiftBar title color-coding by urgency.
# the title text is remaining % (comfortable) or countdown (exceeding),
# colored to signal how urgent the situation is at a glance.
_ANSI_WHITE = "\033[37m"
_ANSI_GREEN = "\033[32m"
_ANSI_YELLOW = "\033[33m"
_ANSI_ORANGE = "\033[38;5;208m"
_ANSI_RED = "\033[31m"
_ANSI_RESET = "\033[0m"


def _urgency_color(minutes_until_limit: float | None) -> tuple[str, str]:
    """pick ANSI escape and SF Symbol hex color based on minutes to limit.

    returns (ansi_escape, hex_color) for the current urgency level:
      no limit projected  →  white / dim gray
      >= 60 minutes       →  white / dim gray
      30-60 minutes       →  yellow
      10-30 minutes       →  orange
      < 10 minutes        →  red
    """
    if minutes_until_limit is None or minutes_until_limit >= 60:
        return _ANSI_WHITE, "#999999"
    if minutes_until_limit >= 30:
        return _ANSI_YELLOW, "#d4a72c"
    if minutes_until_limit >= 10:
        return _ANSI_ORANGE, "#ff9500"
    return _ANSI_RED, "#ff3b30"


def _format_countdown_text(minutes_until_limit: float) -> str:
    """format minutes-to-limit as a compact menu bar countdown.

    produces human-friendly durations like "47m", "1h12m", "8m", "<1m".
    """
    if minutes_until_limit < 1:
        return "<1m"
    total_minutes = int(minutes_until_limit)
    if total_minutes >= 60:
        hours = total_minutes // 60
        remaining_minutes = total_minutes % 60
        if remaining_minutes == 0:
            return "%(h)dh" % {"h": hours}
        return "%(h)dh%(m)dm" % {"h": hours, "m": remaining_minutes}
    return "%(m)dm" % {"m": total_minutes}


def _projection_color(projected_utilization: float) -> str:
    """ANSI color for projected usage normalized to accounts.

    green at ≤1 account (using one account's worth or less),
    yellow at ≤2, orange at ≤3, red at >3 (only 3 accounts exist).
    """
    accounts = projected_utilization / 100.0
    if accounts <= 1.0:
        return _ANSI_GREEN
    if accounts <= 2.0:
        return _ANSI_YELLOW
    if accounts <= 3.0:
        return _ANSI_ORANGE
    return _ANSI_RED


def _format_bar_title_text(
    minutes_until_limit: float | None,
    utilization: float | None,
    projected_remaining: float | None,
) -> str:
    """format the menu bar title: usage percentages with optional countdown.

    primary element is current→projected usage: "42→67%".
    when projected to exceed 100%, appends the countdown: "16→133% (3h4m)".
    the countdown only appears when it's actionable (will hit the limit).

    examples:
      "42→67%"          — comfortable, heading for 67%
      "16→133% (3h4m)"  — exceeding, 3h4m until hitting the wall
      "42%"             — no projection data yet
    """
    if utilization is None:
        return "∞"

    used = min(100, int(utilization))

    if projected_remaining is not None:
        projected_used = max(used, int(100.0 - projected_remaining))
        proj_color = _projection_color(projected_used)
        countdown_suffix = ""
        if projected_used > 100 and minutes_until_limit is not None:
            countdown_suffix = " (%(t)s)" % {"t": _format_countdown_text(minutes_until_limit)}
        return "%(w)s%(u)d→%(c)s%(p)d%%%(cd)s%(r)s" % {
            "w": _ANSI_WHITE,
            "u": used,
            "c": proj_color,
            "p": projected_used,
            "cd": countdown_suffix,
            "r": _ANSI_RESET,
        }

    return "%(w)s%(u)d%%%(r)s" % {"w": _ANSI_WHITE, "u": used, "r": _ANSI_RESET}


def _render_bar_status(
    db_path: Path,
    provider_name: str,
    tracked_window: str,
    poll_interval: int,
) -> str:
    """render SwiftBar-formatted output for the menu bar plugin.

    shows remaining capacity % when comfortable (e.g. "72%"), switching to
    a countdown when projected to exceed (e.g. "47m"). color-coded by
    urgency: white/gray when comfortable, escalating through yellow/orange/red
    as the limit approaches.
    """
    from prism.mac.swiftbar import item, refresh_item, separator, title

    status = _query_status(db_path, provider_name, tracked_window, poll_interval)
    if "error" in status:
        from prism.mac.swiftbar import error_dropdown, error_title

        return error_title() + "\n" + error_dropdown(status["error"])

    # find the tracked window's burn rate
    minutes_until_limit = None
    tracked_utilization = None
    tracked_resets_at = None
    projected_remaining = None

    for window in status["windows"]:
        if window["name"] == tracked_window:
            tracked_utilization = window["utilization"]
            tracked_resets_at = window["resets_at"]
            burn = window.get("burn_rate")
            if burn:
                minutes_until_limit = burn.get("minutes_until_limit")
                projected_remaining = burn.get("projected_remaining_at_reset")
            break

    # only show minutes-to-limit when actually projected to exceed
    will_exceed = projected_remaining is not None and projected_remaining < 0
    effective_minutes = minutes_until_limit if will_exceed else None

    _unused, hex_color = _urgency_color(effective_minutes)
    colored_title = _format_bar_title_text(
        effective_minutes, tracked_utilization, projected_remaining
    )

    lines: list[str] = []
    lines.append(title(colored_title, ansi=True))
    lines.append(separator())

    # utilization line
    if tracked_utilization is not None:
        lines.append(
            item(
                "%(util).0f%% used" % {"util": tracked_utilization},
                color="#ffffff",
                size=13,
            )
        )

    # minutes-to-limit or comfortable status
    if will_exceed and minutes_until_limit is not None:
        lines.append(
            item(
                "limit in %(text)s" % {"text": _format_countdown_text(minutes_until_limit)},
                color=hex_color,
                size=13,
            )
        )
    else:
        lines.append(item("on track to stay under limit", color="#999999", size=13))

    # reset time
    if tracked_resets_at:
        lines.append(
            item(
                "resets %(time)s" % {"time": tracked_resets_at},
                color="#666666",
                size=11,
            )
        )

    # account
    if status.get("account_email"):
        lines.append(item(status["account_email"], color="#666666", size=11))

    lines.append(separator())
    lines.append(refresh_item())

    return "\n".join(lines)


class _StatusHandler(BaseHTTPRequestHandler):
    """HTTP handler for /status, /bar-status, and /health endpoints."""

    db_path: Path
    provider_name: str
    tracked_window: str
    poll_interval: int

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, "text/plain", b"ok")
            return

        if self.path == "/status":
            try:
                status = _query_status(
                    self.db_path,
                    self.provider_name,
                    self.tracked_window,
                    self.poll_interval,
                )
            except Exception as error:
                body = json.dumps({"error": str(error)}).encode()
                self._respond(500, "application/json", body)
                return

            if "error" in status:
                body = json.dumps(status).encode()
                self._respond(503, "application/json", body)
                return

            body = json.dumps(status, default=str).encode()
            self._respond(200, "application/json", body, cors=True)
            return

        if self.path == "/bar-status":
            try:
                body = _render_bar_status(
                    self.db_path,
                    self.provider_name,
                    self.tracked_window,
                    self.poll_interval,
                ).encode()
                self._respond(200, "text/plain; charset=utf-8", body)
            except Exception as error:
                from prism.mac.swiftbar import error_dropdown, error_title

                body = (error_title() + "\n" + error_dropdown(str(error))).encode()
                self._respond(200, "text/plain; charset=utf-8", body)
            return

        self.send_response(404)
        self.end_headers()

    def _respond(
        self,
        code: int,
        content_type: str,
        body: bytes,
        cors: bool = False,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.info("status request", path=self.path, status=args[1] if len(args) > 1 else None)


def start_status_server(
    port: int,
    provider_name: str,
    tracked_window: str,
    poll_interval: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> HTTPServer:
    """start the status HTTP server in a daemon thread."""
    handler_class = type(
        "Handler",
        (_StatusHandler,),
        {
            "db_path": db_path,
            "provider_name": provider_name,
            "tracked_window": tracked_window,
            "poll_interval": poll_interval,
        },
    )
    server = HTTPServer(("127.0.0.1", port), handler_class)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("status server started", port=port, db=str(db_path))
    return server
