from datetime import UTC, datetime, timedelta

from backend.services import pending_slips


def test_save_and_pop_returns_betslip():
    session = {}
    betslip = {"marketId": "1.123", "instructions": []}
    pending_slips.save(session, "abc", betslip)
    assert pending_slips.pop(session, "abc") == betslip


def test_pop_removes_slip():
    session = {}
    pending_slips.save(session, "abc", {"marketId": "1.123", "instructions": []})
    pending_slips.pop(session, "abc")
    assert pending_slips.pop(session, "abc") is None


def test_pop_unknown_id_returns_none():
    assert pending_slips.pop({}, "nonexistent") is None


def test_pop_expired_returns_none():
    session = {}
    pending_slips.save(session, "abc", {"marketId": "1.123", "instructions": []})
    old_time = (datetime.now(UTC) - timedelta(seconds=pending_slips.SLIP_TTL_SECONDS + 1)).isoformat()
    session[pending_slips._STORE_KEY]["abc"]["created_at"] = old_time
    assert pending_slips.pop(session, "abc") is None


def test_expired_slip_is_removed_from_store():
    session = {}
    pending_slips.save(session, "abc", {"marketId": "1.123", "instructions": []})
    old_time = (datetime.now(UTC) - timedelta(seconds=pending_slips.SLIP_TTL_SECONDS + 1)).isoformat()
    session[pending_slips._STORE_KEY]["abc"]["created_at"] = old_time
    pending_slips.pop(session, "abc")
    assert "abc" not in session.get(pending_slips._STORE_KEY, {})
