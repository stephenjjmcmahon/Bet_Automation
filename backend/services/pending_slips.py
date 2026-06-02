from datetime import datetime, timedelta, timezone

SLIP_TTL_SECONDS = 300

# Key used inside the session dict to store pending slips.
_STORE_KEY = "pending_slips"


def save(session: dict, slip_id: str, betslip: dict) -> None:
    # Initialise the per-user slip store if this is their first slip.
    if _STORE_KEY not in session:
        session[_STORE_KEY] = {}
    # created_at is stored as an ISO string because session data is JSON-serialised.
    session[_STORE_KEY][slip_id] = {
        "betslip": betslip,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def get_created_at(session: dict, slip_id: str) -> datetime | None:
    """Return the creation time of a pending slip without removing it, or None if not present."""
    entry = session.get(_STORE_KEY, {}).get(slip_id)
    if entry is None:
        return None
    return datetime.fromisoformat(entry["created_at"])


def pop(session: dict, slip_id: str) -> dict | None:
    """Return and remove the betslip, or None if missing or expired."""
    store = session.get(_STORE_KEY, {})
    entry = store.pop(slip_id, None)
    if entry is None:
        return None
    # Write back the store with the entry removed.
    session[_STORE_KEY] = store
    created_at = datetime.fromisoformat(entry["created_at"])
    if datetime.now(timezone.utc) - created_at > timedelta(seconds=SLIP_TTL_SECONDS):
        return None
    return entry["betslip"]
