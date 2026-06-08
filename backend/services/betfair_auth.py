import os
import requests
from dotenv import load_dotenv

load_dotenv()

BETFAIR_LOGIN_URL = "https://identitysso.betfair.com/api/login"

_SESSION_KEY = "betfair_token"


class SessionExpiredError(Exception):
    pass


def login(username: str, password: str, session: dict) -> None:
    """Call Betfair's Interactive Login endpoint and store the token in the user's session."""
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

    session[_SESSION_KEY] = body["token"]


def get_token(session: dict) -> str:
    """Return the token from this user's session, or raise if not logged in."""
    token = session.get(_SESSION_KEY)
    if token is None:
        raise SessionExpiredError("Not logged in")
    return token


def clear_token(session: dict) -> None:
    """Discard the token from this user's session — called when Betfair returns 401."""
    session.pop(_SESSION_KEY, None)
