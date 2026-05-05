import os
import requests
from dotenv import load_dotenv

load_dotenv()

BETFAIR_ENDPOINT = "https://api.betfair.com/exchange/betting/rest/v1.0/"
BETFAIR_LOGIN_URL = "https://identitysso-cert.betfair.com/api/certlogin"

# In-memory session token (refreshed automatically)
_session_token = None


def login():
    """Log in to Betfair and store a fresh session token."""
    global _session_token

    username = os.getenv("BETFAIR_USERNAME")
    password = os.getenv("BETFAIR_PASSWORD")
    app_key  = os.getenv("BETFAIR_APP_KEY")

    # Non-interactive (bot) login — no cert needed for simple username/password
    response = requests.post(
        "https://identitysso.betfair.com/api/login",
        data={"username": username, "password": password},
        headers={
            "X-Application": app_key,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json"
        }
    )

    data = response.json()

    if data.get("status") != "SUCCESS":
        raise Exception(f"Betfair login failed: {data.get('error', 'Unknown error')}")

    _session_token = data["token"]
    print("Betfair session refreshed successfully.")
    return _session_token


def get_session_token():
    """Return current session token, logging in first if not set."""
    global _session_token
    if not _session_token:
        login()
    return _session_token


def betfair_post(path: str, payload: dict):
    """Make a Betfair API call, auto-retrying once if the session has expired."""
    def _call():
        headers = {
            "X-Application": os.getenv("BETFAIR_APP_KEY"),
            "X-Authentication": get_session_token(),
            "Content-Type": "application/json",
        }
        url = BETFAIR_ENDPOINT + path
        r = requests.post(url, json=payload, headers=headers)
        print("STATUS:", r.status_code)
        print("RESPONSE:", r.text)
        r.raise_for_status()
        return r.json()

    result = _call()

    # Check for expired session in the response body and retry once
    result_str = str(result)
    if "INVALID_SESSION_INFORMATION" in result_str:
        print("Session expired — refreshing and retrying...")
        login()
        result = _call()

    return result


def list_events(team_name: str, event_type_id: str):
    payload = {
        "filter": {
            "eventTypeIds": [event_type_id],
            "textQuery": team_name
        }
    }
    return betfair_post("listEvents/", payload)


def list_market_catalogue(event_id: str, market_type: str = "MATCH_ODDS"):
    payload = {
        "filter": {
            "eventIds": [event_id],
            "marketTypeCodes": [market_type]
        },
        "maxResults": "5",
        "marketProjection": ["RUNNER_DESCRIPTION", "EVENT"]
    }
    return betfair_post("listMarketCatalogue/", payload)


def place_orders(market_id: str, instructions: list):
    payload = {
        "marketId": market_id,
        "instructions": instructions
    }
    return betfair_post("placeOrders/", payload)


def get_best_price(market_id: str, selection_id: int, side: str) -> float:
    payload = {
        "marketIds": [market_id],
        "priceProjection": {
            "priceData": ["EX_BEST_OFFERS"]
        }
    }
    response = betfair_post("listMarketBook/", payload)
    runners = response[0]["runners"]
    for runner in runners:
        if runner["selectionId"] == selection_id:
            ex = runner.get("ex", {})
            offers = ex.get("availableToBack", []) if side == "BACK" else ex.get("availableToLay", [])
            if offers:
                return offers[0]["price"]
    raise ValueError(f"No price available for selection {selection_id}")