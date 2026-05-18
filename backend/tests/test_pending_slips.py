import pytest
from datetime import datetime, timedelta, timezone
from backend.services import pending_slips


@pytest.fixture(autouse=True)
def clear_store():
    pending_slips._store.clear()
    yield
    pending_slips._store.clear()


def test_save_and_pop_returns_betslip():
    betslip = {"marketId": "1.123", "instructions": []}
    pending_slips.save("abc", betslip)
    assert pending_slips.pop("abc") == betslip


def test_pop_removes_slip():
    pending_slips.save("abc", {"marketId": "1.123", "instructions": []})
    pending_slips.pop("abc")
    assert pending_slips.pop("abc") is None


def test_pop_unknown_id_returns_none():
    assert pending_slips.pop("nonexistent") is None


def test_pop_expired_returns_none():
    pending_slips.save("abc", {"marketId": "1.123", "instructions": []})
    pending_slips._store["abc"]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=pending_slips.SLIP_TTL_SECONDS + 1)
    )
    assert pending_slips.pop("abc") is None


def test_expired_slip_is_removed_from_store():
    pending_slips.save("abc", {"marketId": "1.123", "instructions": []})
    pending_slips._store["abc"]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=pending_slips.SLIP_TTL_SECONDS + 1)
    )
    pending_slips.pop("abc")
    assert "abc" not in pending_slips._store
