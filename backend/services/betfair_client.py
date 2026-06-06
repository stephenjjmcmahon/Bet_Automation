import os
import requests
from dotenv import load_dotenv
from backend.services.betfair_auth import get_token, clear_token, SessionExpiredError

load_dotenv()

BETFAIR_ENDPOINT = "https://api.betfair.com/exchange/betting/rest/v1.0/"


def betfair_post(path: str, payload: dict, session: dict):
    headers = {
        "X-Application": os.getenv("BETFAIR_APP_KEY"),
        "X-Authentication": get_token(session),
        "Content-Type": "application/json",
    }

    r = requests.post(BETFAIR_ENDPOINT + path, json=payload, headers=headers)

    if r.status_code == 401:
        clear_token(session)
        raise SessionExpiredError("Betfair session expired — please log in again")

    r.raise_for_status()
    return r.json()


def list_events(team_name: str, event_type_id: str, session: dict):
    payload = {
        "filter": {
            "eventTypeIds": [event_type_id],
            "textQuery": team_name,
        }
    }
    return betfair_post("listEvents/", payload, session)


def list_market_catalogue(event_id: str, market_type: str, session: dict):
    payload = {
        "filter": {
            "eventIds": [event_id],
            "marketTypeCodes": [market_type],
        },
        "maxResults": "5",
        "marketProjection": ["RUNNER_DESCRIPTION", "EVENT", "COMPETITION"],
    }
    return betfair_post("listMarketCatalogue/", payload, session)


def place_orders(market_id: str, instructions: list, session: dict):
    payload = {
        "marketId": market_id,
        "instructions": instructions,
    }
    return betfair_post("placeOrders/", payload, session)


def get_market_book(market_id: str, session: dict) -> dict:
    payload = {
        "marketIds": [market_id],
        "priceProjection": {
            "priceData": ["EX_BEST_OFFERS"],
            "exBestOffersOverrides": {
                "bestPricesDepth": 3,
                "rollupModel": "STAKE",
                "maximumRollup": 10,
            },
        },
    }

    result = betfair_post("listMarketBook/", payload, session)

    if not result:
        raise ValueError(f"No market book returned for market {market_id}")

    return result[0]
