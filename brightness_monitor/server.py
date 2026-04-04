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

from brightness_monitor.storage import (
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
        burn = calculate_burn_rate(connection, provider_name, window_name, resets_at)
        burn_dict = None
        if burn is not None:
            burn_dict = {
                "utilization_per_hour": burn.utilization_per_hour,
                "projected_remaining_at_reset": burn.projected_remaining_at_reset,
                "hours_until_reset": burn.hours_until_reset,
                "sample_minutes": burn.sample_minutes,
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


class _StatusHandler(BaseHTTPRequestHandler):
    """HTTP handler for /status and /health endpoints."""

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
