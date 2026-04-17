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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from prism.logging import get_logger

if TYPE_CHECKING:
    from llm_usage.usage import UsageData

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
    now = datetime.now(tz=UTC).isoformat()

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

    minutes_until_limit: float | None = None
    """estimated minutes until utilization hits 100%. None if not burning
    toward the limit (projected to stay under 100%, rate is zero/negative,
    or insufficient data)."""


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
BURN_RATE_LOOKBACK_MINUTES = 15

# minimum data points needed to calculate a meaningful rate
BURN_RATE_MINIMUM_POLLS = 3


def calculate_burn_rate(
    connection: sqlite3.Connection,
    provider_name: str,
    window_name: str,
    resets_at: datetime | None,
    account_email: str | None = None,
) -> BurnRate:
    """calculate consumption rate from recent poll history.

    looks at the last 30 minutes of polls for the given window,
    computes linear utilization rate, and projects forward to the
    reset time to estimate how many tokens will be left (or wasted).
    scoped to the active account so usage from a previous account
    (pre-switch) doesn't pollute the rate.
    """
    # compute the lookback cutoff in Python so the comparison is between
    # two isoformat strings. SQLite's datetime() returns canonical format
    # (space-separated) which doesn't compare correctly against isoformat
    # timestamps (T-separated) stored in polled_at.
    now = datetime.now(tz=UTC)
    lookback_cutoff = (now - timedelta(minutes=BURN_RATE_LOOKBACK_MINUTES)).isoformat()

    # scope to active account when known, so a mid-session account switch
    # doesn't mix two different utilization curves into the regression
    if account_email:
        rows = connection.execute(
            "SELECT polled_at, utilization FROM usage_polls "
            "WHERE provider = ? "
            "AND window_name = ? "
            "AND account_email = ? "
            "AND polled_at > ? "
            "ORDER BY polled_at ASC",
            (provider_name, window_name, account_email, lookback_cutoff),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT polled_at, utilization FROM usage_polls "
            "WHERE provider = ? "
            "AND window_name = ? "
            "AND polled_at > ? "
            "ORDER BY polled_at ASC",
            (provider_name, window_name, lookback_cutoff),
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

    # least-squares linear fit across all data points for a stable rate.
    # x-axis is hours since the first poll, y-axis is utilization %.
    first_time = datetime.fromisoformat(rows[0][0])
    last_time = datetime.fromisoformat(rows[-1][0])
    sample_minutes = (last_time - first_time).total_seconds() / 60

    if sample_minutes < 0.06:
        # timestamps too close together, can't compute rate
        return BurnRate(
            utilization_per_hour=None,
            projected_remaining_at_reset=None,
            hours_until_reset=hours_until_reset,
            sample_minutes=sample_minutes,
        )

    n = len(rows)
    hours_from_start = []
    utilizations = []
    for timestamp_str, utilization in rows:
        t = datetime.fromisoformat(timestamp_str)
        hours_from_start.append((t - first_time).total_seconds() / 3600)
        utilizations.append(utilization)

    # ordinary least-squares: slope = (n*Σxy - Σx*Σy) / (n*Σx² - (Σx)²)
    sum_x = sum(hours_from_start)
    sum_y = sum(utilizations)
    sum_xy = sum(x * y for x, y in zip(hours_from_start, utilizations, strict=True))
    sum_x2 = sum(x * x for x in hours_from_start)

    denominator = n * sum_x2 - sum_x * sum_x
    if abs(denominator) < 1e-12:
        return BurnRate(
            utilization_per_hour=None,
            projected_remaining_at_reset=None,
            hours_until_reset=hours_until_reset,
            sample_minutes=sample_minutes,
        )

    utilization_per_hour = (n * sum_xy - sum_x * sum_y) / denominator
    # intercept at first_time: y = intercept + slope * x
    intercept = (sum_y - utilization_per_hour * sum_x) / n

    # current fitted value (at the last data point's time)
    last_hours = hours_from_start[-1]
    fitted_now = intercept + utilization_per_hour * last_hours

    # project forward to reset time — utilization can only accumulate,
    # so clamp the projection to at least current usage even if the
    # observed rate is negative (e.g. old usage rolling off the window)
    last_util = utilizations[-1]
    projected_remaining_at_reset = None
    if hours_until_reset is not None:
        projected_utilization = max(
            last_util,
            fitted_now + (utilization_per_hour * hours_until_reset),
        )
        projected_remaining_at_reset = 100.0 - projected_utilization

    # estimate minutes until hitting 100% utilization — only meaningful
    # when actively burning toward the limit (positive rate, not already past it)
    minutes_until_limit = None
    if utilization_per_hour > 0 and last_util < 100.0:
        hours_to_limit = (100.0 - fitted_now) / utilization_per_hour
        minutes_until_limit = max(0.0, hours_to_limit * 60)

    return BurnRate(
        utilization_per_hour=utilization_per_hour,
        projected_remaining_at_reset=projected_remaining_at_reset,
        hours_until_reset=hours_until_reset,
        sample_minutes=sample_minutes,
        minutes_until_limit=minutes_until_limit,
    )
