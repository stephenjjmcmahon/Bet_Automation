from datetime import datetime, timedelta, timezone

SLIP_TTL_SECONDS = 300  # slips expire after 5 minutes

_store: dict[str, dict] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def save(slip_id: str, betslip: dict) -> None:
    _store[slip_id] = {"betslip": betslip, "created_at": _now()}


def get_created_at(slip_id: str) -> datetime | None:
    """Return the creation time of a pending slip without removing it, or None if not present."""
    entry = _store.get(slip_id)
    return entry["created_at"] if entry else None


def pop(slip_id: str) -> dict | None:
    """Return and remove the betslip, or None if missing or expired."""
    entry = _store.pop(slip_id, None)
    if entry is None:
        return None
    if _now() - entry["created_at"] > timedelta(seconds=SLIP_TTL_SECONDS):
        return None
    return entry["betslip"]
