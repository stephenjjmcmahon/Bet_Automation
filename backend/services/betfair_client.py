import os
from datetime import datetime, timezone

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

    if r.status_code == 401 or "INVALID_SESSION_INFORMATION" in r.text:
        clear_token(session)
        raise SessionExpiredError("Betfair session expired — please log in again")

    if not r.ok:
        raise ValueError(f"Betfair {r.status_code} error on {path}: {r.text}")

    return r.json()


def list_market_types_for_events(event_ids: list[str], session: dict) -> list[str]:
    """Market types available across a batch of specific events (one API call)."""
    result = betfair_post(
        "listMarketTypes/",
        {"filter": {"eventIds": event_ids}},
        session,
    )
    return [entry["marketType"] for entry in result]


def list_market_types_for_sport(event_type_id: str, session: dict) -> list[str]:
    """All market types for a sport."""
    result = betfair_post(
        "listMarketTypes/",
        {"filter": {"eventTypeIds": [event_type_id]}},
        session,
    )
    return [entry["marketType"] for entry in result]


def list_events(team_name: str, event_type_id: str, session: dict):
    payload = {
        "filter": {
            "eventTypeIds": [event_type_id],
            "textQuery": team_name,
        }
    }
    return betfair_post("listEvents/", payload, session)


def list_market_catalogue(event_id: str, market_type: str, session: dict):
    # maxResults is high enough to return every market of the type for an event:
    # some types span several markets that differ only by line (e.g. AFL
    # WINNING_MARGIN 24.5 / 39.5 / spread). MARKET_DESCRIPTION lets the resolver
    # tell those apart so it can reach more than the first one.
    payload = {
        "filter": {
            "eventIds": [event_id],
            "marketTypeCodes": [market_type],
        },
        "maxResults": "20",
        "marketProjection": ["RUNNER_DESCRIPTION", "EVENT", "COMPETITION", "MARKET_DESCRIPTION"],
    }
    return betfair_post("listMarketCatalogue/", payload, session) # Get the runners list here


def list_racing_markets(event_type_id: str, market_type: str, session: dict) -> list:
    """All upcoming races for a racing sport in one call.

    Racing events are meetings; each race is a market beneath one. Fetching
    every market of the given type across the sport (a busy day is ~450 WIN
    markets, well under the 1000 cap) lets the resolver find the race by
    runner (horse/dog) name without knowing the meeting. `from: now` drops
    races already run; no upper bound so bets days ahead still resolve.
    Ante-post markets are a separate market type (ANTEPOST_WIN), so they
    never leak into a WIN scan.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "filter": {
            "eventTypeIds": [event_type_id],
            "marketTypeCodes": [market_type],
            "marketStartTime": {"from": now},
        },
        "maxResults": "1000",
        "marketProjection": ["RUNNER_DESCRIPTION", "EVENT", "MARKET_START_TIME"],
        "sort": "FIRST_TO_START",
    }
    return betfair_post("listMarketCatalogue/", payload, session)


def list_place_markets_for_event(event_id: str, session: dict) -> list:
    """All place markets for one race meeting: the standard PLACE ('To Be Placed')
    plus the OTHER_PLACE alternates ('2 TBP', '4 TBP'). Includes MARKET_DESCRIPTION
    so the caller can tell the standard market (PLACE) from the alternates."""
    payload = {
        "filter": {
            "eventIds": [event_id],
            "marketTypeCodes": ["PLACE", "OTHER_PLACE"],
        },
        "maxResults": "30",
        "marketProjection": ["RUNNER_DESCRIPTION", "MARKET_DESCRIPTION", "MARKET_START_TIME", "EVENT"],
        "sort": "FIRST_TO_START",
    }
    return betfair_post("listMarketCatalogue/", payload, session)


def get_market_winners(market_ids: list[str], session: dict) -> dict:
    """Map marketId → numberOfWinners (places paid). Only on the market book, not
    the catalogue, so this is a separate call."""
    if not market_ids:
        return {}
    books = betfair_post("listMarketBook/", {"marketIds": market_ids}, session)
    return {b["marketId"]: b.get("numberOfWinners") for b in books}


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
