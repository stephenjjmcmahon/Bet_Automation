"""
Structured event logger using SQLite.

Every significant action in the bet flow — slip prepared, bet confirmed,
slip expired, prepare failed — writes one row to the bet_events table.
The slip_id column links a slip_prepared row to its bet_confirmed or
slip_expired row, so time_to_confirm can be queried directly.

Switching to PostgreSQL later requires only changing DB_PATH to a connection
string and swapping sqlite3 for psycopg2/asyncpg — the table schema and
log_event call sites stay identical.

Pilot metrics available from this table
----------------------------------------
Time-to-bet:
    SELECT AVG(time_to_slip_ms) FROM bet_events WHERE event_type = 'slip_prepared'

Completion rate:
    SELECT
        SUM(CASE WHEN event_type = 'bet_confirmed' THEN 1 ELSE 0 END) * 1.0
        / SUM(CASE WHEN event_type = 'slip_prepared' THEN 1 ELSE 0 END)
    FROM bet_events

Total handle (GBP staked on confirmed bets):
    SELECT SUM(stake) FROM bet_events WHERE event_type = 'bet_confirmed'

Average time to confirm:
    SELECT AVG(time_to_confirm_ms) FROM bet_events WHERE event_type = 'bet_confirmed'
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "betting.db"

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS bet_events (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type            TEXT    NOT NULL,
        timestamp             TEXT    NOT NULL,
        slip_id               TEXT,
        time_to_slip_ms       INTEGER,
        time_to_confirm_ms    INTEGER,
        selection_name        TEXT,
        side                  TEXT,
        stake                 REAL,
        price                 REAL,
        market_id             TEXT,
        event_id              TEXT,
        event_start_time      TEXT,
        time_to_event_seconds INTEGER,
        reason                TEXT
    )
"""

_INSERT = """
    INSERT INTO bet_events (
        event_type, timestamp, slip_id,
        time_to_slip_ms, time_to_confirm_ms,
        selection_name, side, stake, price,
        market_id, event_id, event_start_time,
        time_to_event_seconds, reason
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def log_event(event_type: str, **fields) -> None:
    """
    Write one structured row to the bet_events table.

    Parameters
    ----------
    event_type : str
        One of: slip_prepared | bet_confirmed | slip_expired | prepare_failed
    **fields
        Any subset of the bet_events columns. Columns not supplied are NULL.
        Extra keys not matching any column are silently ignored.

    The database file and logs/ directory are created on first call.
    WAL journal mode is enabled for better concurrent read performance.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_CREATE_TABLE)
        conn.execute(_INSERT, (
            event_type,
            datetime.now(timezone.utc).isoformat(),
            fields.get("slip_id"),
            fields.get("time_to_slip_ms"),
            fields.get("time_to_confirm_ms"),
            fields.get("selection_name"),
            fields.get("side"),
            fields.get("stake"),
            fields.get("price"),
            fields.get("market_id"),
            fields.get("event_id"),
            fields.get("event_start_time"),
            fields.get("time_to_event_seconds"),
            fields.get("reason"),
        ))
        conn.commit()
    finally:
        conn.close()
