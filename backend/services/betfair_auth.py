import os
import requests
from dotenv import load_dotenv

load_dotenv()

BETFAIR_LOGIN_URL = "https://identitysso.betfair.com/api/login"

# Token lives in memory for the lifetime of the server process.
# It's cleared on logout or when Betfair returns a 401.
_session_token: str | None = None


def login(username: str, password: str) -> None:
    """Call Betfair's Interactive Login endpoint and cache the session token."""
    global _session_token

    app_key = os.getenv("BETFAIR_APP_KEY")

    # Betfair requires form-encoded body, not JSON
    response = requests.post(
        BETFAIR_LOGIN_URL,
        data={"username": username, "password": password},
        headers={
            "X-Application": app_key,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )

    response.raise_for_status()
    body = response.json()

    # HTTP 200 doesn't mean success — Betfair signals failures in the body
    if body.get("status") != "SUCCESS":
        error = body.get("error", "UNKNOWN_ERROR")
        raise ValueError(f"Betfair login failed: {error}")

    _session_token = body["token"]


def get_token() -> str:
    """Return the cached token, or raise if no active session."""
    if _session_token is None:
        raise SessionExpiredError("Not logged in")
    return _session_token


def clear_token() -> None:
    """Discard the cached token — called when Betfair returns 401."""
    global _session_token
    _session_token = None


class SessionExpiredError(Exception):
    pass
