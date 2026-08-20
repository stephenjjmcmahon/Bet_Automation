import sqlite3
from datetime import datetime

import pytest

from backend.services import logger


@pytest.fixture
def db(tmp_path, monkeypatch):
    tmp_db = tmp_path / "test_betting.db"
    monkeypatch.setattr(logger, "DB_PATH", tmp_db)
    return tmp_db


def _bets(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM bets").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _failures(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM failures").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _prepared(db, **overrides):
    kwargs = {
        "slip_id": "abc",
        "time_to_slip_ms": 1000,
        "selection_name": "Arsenal",
        "side": "BACK",
        "stake": 10.0,
        "price": 3.0,
        "market_id": "1.123",
        "event_id": "123",
    }
    kwargs.update(overrides)
    logger.log_slip_prepared(**kwargs)


# --- table creation ---

def test_creates_both_tables_on_first_call(db):
    logger.log_failure(reason="no_matching_event")
    assert db.exists()
    conn = sqlite3.connect(str(db))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert "bets" in tables
    assert "failures" in tables


# --- log_slip_prepared ---

def test_slip_prepared_inserts_row(db):
    _prepared(db)
    rows = _bets(db)
    assert len(rows) == 1
    assert rows[0]["slip_id"] == "abc"
    assert rows[0]["status"] == "prepared"
    assert rows[0]["selection_name"] == "Arsenal"


def test_slip_prepared_at_is_iso_format(db):
    _prepared(db)
    datetime.fromisoformat(_bets(db)[0]["prepared_at"])


def test_slip_prepared_numeric_fields(db):
    _prepared(db, time_to_slip_ms=1234, stake=50.0, price=2.5, time_to_event_seconds=3600)
    row = _bets(db)[0]
    assert row["time_to_slip_ms"] == 1234
    assert row["stake"] == 50.0
    assert row["price"] == 2.5
    assert row["time_to_event_seconds"] == 3600


def test_slip_prepared_optional_fields_default_null(db):
    _prepared(db)
    row = _bets(db)[0]
    assert row["event_start_time"] is None
    assert row["time_to_event_seconds"] is None


# --- log_bet_confirmed ---

def test_bet_confirmed_updates_status(db):
    _prepared(db)
    logger.log_bet_confirmed(slip_id="abc", time_to_confirm_ms=5000)
    row = _bets(db)[0]
    assert row["status"] == "confirmed"
    assert row["time_to_confirm_ms"] == 5000
    assert row["confirmed_at"] is not None


def test_bet_confirmed_at_is_iso_format(db):
    _prepared(db)
    logger.log_bet_confirmed(slip_id="abc", time_to_confirm_ms=5000)
    datetime.fromisoformat(_bets(db)[0]["confirmed_at"])


def test_bet_confirmed_does_not_affect_other_slips(db):
    _prepared(db, slip_id="a")
    _prepared(db, slip_id="b")
    logger.log_bet_confirmed(slip_id="a", time_to_confirm_ms=5000)
    rows = {r["slip_id"]: r for r in _bets(db)}
    assert rows["a"]["status"] == "confirmed"
    assert rows["b"]["status"] == "prepared"


# --- log_slip_expired ---

def test_slip_expired_updates_status(db):
    _prepared(db)
    logger.log_slip_expired(slip_id="abc")
    row = _bets(db)[0]
    assert row["status"] == "expired"
    assert row["expired_at"] is not None


def test_slip_expired_at_is_iso_format(db):
    _prepared(db)
    logger.log_slip_expired(slip_id="abc")
    datetime.fromisoformat(_bets(db)[0]["expired_at"])


# --- log_failure ---

def test_failure_inserts_row(db):
    logger.log_failure(reason="no_matching_event", selection_name="Arsenal", stake=1.0)
    rows = _failures(db)
    assert len(rows) == 1
    assert rows[0]["reason"] == "no_matching_event"
    assert rows[0]["selection_name"] == "Arsenal"


def test_failure_timestamp_is_iso_format(db):
    logger.log_failure(reason="no_matching_event")
    datetime.fromisoformat(_failures(db)[0]["timestamp"])


def test_failure_optional_fields_default_null(db):
    logger.log_failure(reason="no_matching_event")
    row = _failures(db)[0]
    assert row["market_id"] is None
    assert row["event_id"] is None
    assert row["selection_name"] is None


def test_multiple_failures_append(db):
    logger.log_failure(reason="no_matching_event")
    logger.log_failure(reason="market_suspended", market_id="1.123")
    assert len(_failures(db)) == 2
