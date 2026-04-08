import os
import requests
from dotenv import load_dotenv
load_dotenv()

BETFAIR_ENDPOINT = "https://api.betfair.com/exchange/betting/rest/v1.0/"

def betfair_post(path: str, payload: dict):

    headers = {
        "X-Application": os.getenv("BETFAIR_APP_KEY"),
        "X-Authentication": os.getenv("BETFAIR_SESSION_TOKEN"),
        "Content-Type": "application/json",
    }

    url = BETFAIR_ENDPOINT + path

    r = requests.post(url, json=payload, headers=headers)

    print("STATUS:", r.status_code)
    print("RESPONSE:", r.text)

    r.raise_for_status()

    return r.json()

def list_events(team_name: str, event_type_id: str):

    payload = {
        "filter": {
            "eventTypeIds": [event_type_id],  # 1 = Soccer
            "textQuery": team_name
        }
    }

    return betfair_post("listEvents/", payload)

def list_market_catalogue(event_id: str, market_type: str = "MATCH_ODDS"):

    payload = {
        "filter": {
            "eventIds": [event_id],
            "marketTypeCodes": [market_type]            ### Only works with footbal TODO Change ai to work with other sports market definitions
        },
        "maxResults": "5",
        "marketProjection": ["RUNNER_DESCRIPTION", "EVENT"]

    }
    #print("Markets returned:", payload)
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
            if side == "BACK":
                offers = ex.get("availableToBack", [])
            else:
                offers = ex.get("availableToLay", [])
            if offers:
                return offers[0]["price"]  # Best available price
    raise ValueError(f"No price available for selection {selection_id}")