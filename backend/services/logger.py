import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "betting.db"

# One row per slip — updated in place as the bet moves through its lifecycle.
# slip_id is the primary key so confirmed/expired UPDATEs hit the right row.
# Columns nullable by design: confirmed_at/expired_at are NULL until that event happens;
# event_start_time/time_to_event_seconds are NULL if Betfair didn't return a start time.
_CREATE_BETS = """
    CREATE TABLE IF NOT EXISTS bets (
        slip_id               TEXT PRIMARY KEY,
        status                TEXT NOT NULL DEFAULT 'prepared',  -- prepared | confirmed | expired
        prepared_at           TEXT NOT NULL,
        confirmed_at          TEXT,
        expired_at            TEXT,
        time_to_slip_ms       INTEGER NOT NULL,   -- how long AI + search took
        time_to_confirm_ms    INTEGER,            -- how long the user took to confirm
        selection_name        TEXT NOT NULL,
        side                  TEXT NOT NULL,      -- BACK | LAY
        stake                 REAL NOT NULL,
        price                 REAL NOT NULL,
        market_id             TEXT NOT NULL,
        event_id              TEXT NOT NULL,
        event_start_time      TEXT,
        time_to_event_seconds INTEGER
    )
"""

# Requests that never produced a slip — separate table because these have no slip_id
# and a completely different set of relevant fields to bets.
# market_id and event_id are nullable: early failures happen before those are resolved.
_CREATE_FAILURES = """
    CREATE TABLE IF NOT EXISTS failures (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp         TEXT NOT NULL,
        reason            TEXT NOT NULL,    -- no_matching_event | market_resolution_failed | market_suspended | insufficient_liquidity
        selection_name    TEXT,
        stake             REAL,
        market_id         TEXT,             -- NULL if failure was before market resolution
        event_id          TEXT              -- NULL if failure was before event was found
    )
"""


def _get_conn():
    # Creates the logs/ directory and both tables if they don't already exist,
    # then returns an open connection ready to use.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")  # better concurrent read performance
    conn.execute(_CREATE_BETS)
    conn.execute(_CREATE_FAILURES)
    return conn


def log_slip_prepared(
    slip_id: str,
    time_to_slip_ms: int,
    selection_name: str,
    side: str,
    stake: float,
    price: float,
    market_id: str,
    event_id: str,
    event_start_time: str | None = None,
    time_to_event_seconds: int | None = None,
) -> None:
    # INSERT a new row — status starts as 'prepared' and is updated later on confirm/expire.
    prepared_at = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO bets (
                slip_id, status, prepared_at, time_to_slip_ms,
                selection_name, side, stake, price,
                market_id, event_id, event_start_time, time_to_event_seconds
            ) VALUES (?, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slip_id, prepared_at, time_to_slip_ms,
                selection_name, side, stake, price,
                market_id, event_id, event_start_time, time_to_event_seconds,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def log_bet_confirmed(slip_id: str, time_to_confirm_ms: int | None) -> None:
    # UPDATE the existing row rather than inserting — all the bet details are already there.
    confirmed_at = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE bets SET status='confirmed', confirmed_at=?, time_to_confirm_ms=? WHERE slip_id=?",
            (confirmed_at, time_to_confirm_ms, slip_id),
        )
        conn.commit()
    finally:
        conn.close()


def log_slip_expired(slip_id: str) -> None:
    # UPDATE the existing row — slip timed out before the user confirmed.
    expired_at = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE bets SET status='expired', expired_at=? WHERE slip_id=?",
            (expired_at, slip_id),
        )
        conn.commit()
    finally:
        conn.close()


def log_failure(
    reason: str,
    selection_name: str | None = None,
    stake: float | None = None,
    market_id: str | None = None,
    event_id: str | None = None,
) -> None:
    # INSERT into the separate failures table — these requests never produced a slip.
    timestamp = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO failures (timestamp, reason, selection_name, stake, market_id, event_id) VALUES (?, ?, ?, ?, ?, ?)",
            (timestamp, reason, selection_name, stake, market_id, event_id),
        )
        conn.commit()
    finally:
        conn.close()
