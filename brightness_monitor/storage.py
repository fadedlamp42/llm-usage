"""SQLite storage for usage poll history.

stores every poll result as one row per usage window and provider.
the database lives in the repo root so it can be version-controlled
as a lightweight backup of usage history.

also provides burn rate analysis by looking at recent poll history
to calculate consumption rate and project forward to window reset.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from prism.logging import get_logger

if TYPE_CHECKING:
    from brightness_monitor.usage import UsageData

logger = get_logger()

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "usage.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL DEFAULT 'claude',
    polled_at TEXT NOT NULL,
    window_name TEXT NOT NULL,
    utilization REAL NOT NULL,
    remaining REAL NOT NULL,
    resets_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_polls_polled_at
    ON usage_polls (polled_at);

CREATE INDEX IF NOT EXISTS idx_polls_window_name
    ON usage_polls (window_name);
"""

PROVIDER_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_polls_provider_window_polled_at "
    "ON usage_polls (provider, window_name, polled_at)"
)

ACCOUNT_EMAIL_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_polls_account_email ON usage_polls (account_email)"
)


def _migrate_add_column_if_missing(
    connection: sqlite3.Connection,
    column_name: str,
    column_definition: str,
) -> None:
    """add a column to usage_polls if it doesn't exist yet."""
    columns = connection.execute("PRAGMA table_info(usage_polls)").fetchall()
    column_names = {column[1] for column in columns}
    if column_name in column_names:
        return

    connection.execute(
        "ALTER TABLE usage_polls ADD COLUMN %(column)s %(definition)s"
        % {"column": column_name, "definition": column_definition}
    )
    connection.commit()
    logger.info("migrated usage_polls table", added_column=column_name)


def initialize_database(db_path: Path | None = None) -> sqlite3.Connection:
    """open (or create) the usage database and ensure schema exists.

    returns a persistent connection for the daemon's lifetime.
    """
    path = db_path or DEFAULT_DB_PATH
    logger.info("opening usage database", path=str(path))

    connection = sqlite3.connect(str(path))
    connection.executescript(SCHEMA)

    # incremental migrations for columns added after initial schema
    _migrate_add_column_if_missing(connection, "provider", "TEXT NOT NULL DEFAULT 'claude'")
    _migrate_add_column_if_missing(connection, "account_email", "TEXT")

    connection.execute(PROVIDER_INDEX_SQL)
    connection.execute(ACCOUNT_EMAIL_INDEX_SQL)
    connection.commit()

    return connection


def record_poll(
    connection: sqlite3.Connection,
    usage: UsageData,
    provider_name: str,
) -> None:
    """insert one row per usage window for the current poll."""
    now = datetime.now(tz=timezone.utc).isoformat()

    rows = [
        (
            provider_name,
            now,
            window.name,
            window.utilization,
            100.0 - window.utilization,
            window.resets_at.isoformat() if window.resets_at else None,
            usage.account_email,
        )
        for window in usage.windows
    ]

    connection.executemany(
        "INSERT INTO usage_polls "
        "(provider, polled_at, window_name, utilization, remaining, resets_at, account_email) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.commit()

    logger.debug(
        "recorded usage windows",
        provider=provider_name,
        account=usage.account_email or "unknown",
        count=len(rows),
        time=now,
    )


@dataclass
class BurnRate:
    """usage consumption rate and projection for a single window."""

    utilization_per_hour: float | None
    """% consumed per hour based on recent history. None if insufficient data."""

    projected_remaining_at_reset: float | None
    """projected % remaining when window resets. None if no reset time or no rate."""

    hours_until_reset: float | None
    """hours until window resets. None if no reset time."""

    sample_minutes: float
    """how many minutes of history the rate was calculated from."""


@dataclass
class AccountUtilization:
    """last known utilization for an account on a specific window."""

    account_email: str
    utilization: float
    remaining: float
    polled_at: str


def get_alternative_account_utilizations(
    connection: sqlite3.Connection,
    window_name: str,
    current_email: str,
    candidate_emails: list[str],
) -> list[AccountUtilization]:
    """find the most recent utilization for each candidate account.

    returns only accounts we have data for, sorted by remaining
    capacity (most remaining first). accounts with no historical
    data are not returned — the caller can infer those are "unknown".
    """
    alternatives = [email for email in candidate_emails if email != current_email]
    if not alternatives:
        return []

    placeholders = ", ".join("?" for _ in alternatives)
    query = (
        "SELECT account_email, utilization, remaining, polled_at "
        "FROM usage_polls "
        "WHERE window_name = ? "
        "AND account_email IN (%(placeholders)s) "
        "GROUP BY account_email "
        "HAVING polled_at = MAX(polled_at) "
        "ORDER BY remaining DESC" % {"placeholders": placeholders}
    )

    rows = connection.execute(query, [window_name, *alternatives]).fetchall()

    return [
        AccountUtilization(
            account_email=row[0],
            utilization=row[1],
            remaining=row[2],
            polled_at=row[3],
        )
        for row in rows
    ]


# how far back to look for burn rate calculation
BURN_RATE_LOOKBACK_MINUTES = 30

# minimum data points needed to calculate a meaningful rate
BURN_RATE_MINIMUM_POLLS = 3


def calculate_burn_rate(
    connection: sqlite3.Connection,
    provider_name: str,
    window_name: str,
    resets_at: datetime | None,
) -> BurnRate:
    """calculate consumption rate from recent poll history.

    looks at the last 30 minutes of polls for the given window,
    computes linear utilization rate, and projects forward to the
    reset time to estimate how many tokens will be left (or wasted).
    """
    cutoff = datetime.now(tz=timezone.utc).isoformat()
    lookback_seconds = BURN_RATE_LOOKBACK_MINUTES * 60

    rows = connection.execute(
        "SELECT polled_at, utilization FROM usage_polls "
        "WHERE provider = ? "
        "AND window_name = ? "
        "AND polled_at > datetime(?, '-%d seconds') "
        "ORDER BY polled_at ASC" % lookback_seconds,
        (provider_name, window_name, cutoff),
    ).fetchall()

    # compute hours until reset
    hours_until_reset = None
    if resets_at is not None:
        now = datetime.now(tz=resets_at.tzinfo)
        seconds_left = (resets_at - now).total_seconds()
        hours_until_reset = max(0.0, seconds_left / 3600)

    if len(rows) < BURN_RATE_MINIMUM_POLLS:
        return BurnRate(
            utilization_per_hour=None,
            projected_remaining_at_reset=None,
            hours_until_reset=hours_until_reset,
            sample_minutes=0.0,
        )

    # use first and last data points for rate
    first_time = datetime.fromisoformat(rows[0][0])
    last_time = datetime.fromisoformat(rows[-1][0])
    first_util = rows[0][1]
    last_util = rows[-1][1]

    elapsed_hours = (last_time - first_time).total_seconds() / 3600
    sample_minutes = (last_time - first_time).total_seconds() / 60

    if elapsed_hours < 0.001:
        # timestamps too close together, can't compute rate
        return BurnRate(
            utilization_per_hour=None,
            projected_remaining_at_reset=None,
            hours_until_reset=hours_until_reset,
            sample_minutes=sample_minutes,
        )

    utilization_per_hour = (last_util - first_util) / elapsed_hours

    # project forward to reset time
    projected_remaining_at_reset = None
    if hours_until_reset is not None:
        projected_utilization = last_util + (utilization_per_hour * hours_until_reset)
        projected_remaining_at_reset = 100.0 - projected_utilization

    return BurnRate(
        utilization_per_hour=utilization_per_hour,
        projected_remaining_at_reset=projected_remaining_at_reset,
        hours_until_reset=hours_until_reset,
        sample_minutes=sample_minutes,
    )
