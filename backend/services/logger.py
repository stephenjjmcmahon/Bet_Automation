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

# One row per natural-language search query run through SearchAgent — separate
# from bets/failures because a search is browsing, not a bet (a search that the
# user later turns into a slip lands in `bets` via prepare-from-market, unlinked).
# Booleans are stored as INTEGER 0/1. llm_latency_ms is the summed time inside
# llm.complete(); the rest of total_latency_ms is Betfair calls + overhead.
_CREATE_SEARCHES = """
    CREATE TABLE IF NOT EXISTS searches (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp         TEXT NOT NULL,
        query             TEXT NOT NULL,
        rounds            INTEGER NOT NULL,   -- tool-calling rounds used (vs MAX_TOOL_ROUNDS)
        hit_round_cap     INTEGER NOT NULL,   -- 1 if it ran out of rounds without present_results
        price_calls       INTEGER NOT NULL,   -- price_markets calls made (vs MAX_PRICE_CALLS)
        total_latency_ms  INTEGER NOT NULL,   -- whole SearchAgent.run wall time
        llm_latency_ms    INTEGER NOT NULL,   -- summed time inside llm.complete()
        cards             INTEGER NOT NULL,   -- bettable cards returned
        events            INTEGER NOT NULL,   -- navigable events returned
        salvaged          INTEGER NOT NULL,   -- 1 if it never called present_results (rescued on exit)
        feedback          INTEGER             -- user rating: NULL = not rated, 1 = thumbs up, 0 = thumbs down
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
    conn.execute(_CREATE_SEARCHES)
    # Migration: add `feedback` to pre-existing `searches` tables, since
    # CREATE TABLE IF NOT EXISTS won't add a column to a table that's already there.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(searches)").fetchall()]
    if "feedback" not in cols:
        conn.execute("ALTER TABLE searches ADD COLUMN feedback INTEGER")
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


def log_search(
    query: str,
    rounds: int,
    hit_round_cap: bool,
    price_calls: int,
    total_latency_ms: int,
    llm_latency_ms: int,
    cards: int,
    events: int,
    salvaged: bool,
) -> int:
    # INSERT one row per search query — booleans coerced to 0/1. Returns the new
    # row's id so the frontend can attach a thumbs up/down rating to it later.
    timestamp = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO searches (
                timestamp, query, rounds, hit_round_cap, price_calls,
                total_latency_ms, llm_latency_ms, cards, events, salvaged
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp, query, rounds, int(hit_round_cap), price_calls,
                total_latency_ms, llm_latency_ms, cards, events, int(salvaged),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def log_search_feedback(search_id: int, correct: bool) -> None:
    # UPDATE the existing search row with the user's rating (1 = up, 0 = down).
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE searches SET feedback=? WHERE id=?",
            (int(correct), search_id),
        )
        conn.commit()
    finally:
        conn.close()
