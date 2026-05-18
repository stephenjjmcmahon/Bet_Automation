import sqlite3
import pytest
from backend.services import logger


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point the logger at a temp database for each test."""
    tmp_db = tmp_path / "test_betting.db"
    monkeypatch.setattr(logger, "DB_PATH", tmp_db)
    return tmp_db


def _rows(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM bet_events").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- table creation ---

def test_creates_database_and_table_on_first_call(db):
    logger.log_event("slip_prepared", slip_id="abc")
    assert db.exists()
    rows = _rows(db)
    assert len(rows) == 1


# --- field mapping ---

def test_event_type_and_slip_id_are_stored(db):
    logger.log_event("bet_confirmed", slip_id="xyz123")
    row = _rows(db)[0]
    assert row["event_type"] == "bet_confirmed"
    assert row["slip_id"] == "xyz123"


def test_timestamp_is_iso_format(db):
    logger.log_event("slip_prepared")
    row = _rows(db)[0]
    from datetime import datetime
    datetime.fromisoformat(row["timestamp"])  # raises if not valid ISO


def test_numeric_fields_are_stored(db):
    logger.log_event(
        "slip_prepared",
        slip_id="abc",
        time_to_slip_ms=1234,
        stake=50.0,
        price=2.5,
        time_to_event_seconds=3600,
    )
    row = _rows(db)[0]
    assert row["time_to_slip_ms"] == 1234
    assert row["stake"] == 50.0
    assert row["price"] == 2.5
    assert row["time_to_event_seconds"] == 3600


def test_unprovided_fields_are_null(db):
    logger.log_event("prepare_failed", reason="no_matching_event")
    row = _rows(db)[0]
    assert row["slip_id"] is None
    assert row["stake"] is None
    assert row["market_id"] is None


def test_reason_field_stored(db):
    logger.log_event("prepare_failed", reason="market_suspended", selection_name="Arsenal")
    row = _rows(db)[0]
    assert row["reason"] == "market_suspended"
    assert row["selection_name"] == "Arsenal"


# --- append behaviour ---

def test_multiple_calls_append_rows(db):
    logger.log_event("slip_prepared", slip_id="a")
    logger.log_event("bet_confirmed", slip_id="a")
    logger.log_event("slip_prepared", slip_id="b")
    rows = _rows(db)
    assert len(rows) == 3
    assert rows[0]["event_type"] == "slip_prepared"
    assert rows[1]["event_type"] == "bet_confirmed"


def test_slip_id_links_prepare_to_confirm(db):
    logger.log_event("slip_prepared", slip_id="link-me", stake=10.0, price=2.0)
    logger.log_event("bet_confirmed", slip_id="link-me", time_to_confirm_ms=5000)
    rows = {r["event_type"]: r for r in _rows(db)}
    assert rows["slip_prepared"]["slip_id"] == rows["bet_confirmed"]["slip_id"]
