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
   # print("RESPONSE:", r.text)

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


def get_market_book(market_id: str) -> dict:
    """
    Fetch a live snapshot of a single market from the Betfair Exchange.

    Calls the Betfair REST API endpoint `listMarketBook`, which returns the
    current state of a market including the best available back and lay prices
    (the order book) for every runner, plus market status and liquidity.

    Payload sent to Betfair
    -----------------------
    {
        "marketIds": ["1.234567"],
        "priceProjection": {
            "priceData": ["EX_BEST_OFFERS"],
            "exBestOffersOverrides": {
                "bestPricesDepth": 3,
                "rollupModel": "STAKE",
                "maximumRollup": 10
            }
        }
    }

    priceData options
    -----------------
    - EX_BEST_OFFERS   : best available back and lay prices for each runner (what we use)
    - EX_ALL_OFFERS    : full depth of book, all price levels
    - EX_TRADED        : total matched volume at each price level
    - SP_TRADED        : starting price traded volume (horse racing only)

    exBestOffersOverrides
    ---------------------
    - bestPricesDepth  : how many price levels to return per side (3 = top 3 back + top 3 lay)
    - rollupModel      : how to aggregate liquidity. STAKE means each price level shows
                         how much total stake is available at that price.
    - maximumRollup    : cap on how much to aggregate per price level (£10 here).

    Response structure (single market, simplified)
    -----------------------------------------------
    {
        "marketId": "1.234567",
        "status": "OPEN",           # OPEN | SUSPENDED | CLOSED | INACTIVE
        "inplay": false,
        "runners": [
            {
                "selectionId": 987654,
                "status": "ACTIVE",   # ACTIVE | WINNER | LOSER | REMOVED
                "lastPriceTraded": 5.1,
                "ex": {
                    "availableToBack": [
                        {"price": 5.2, "size": 150.0},   # best back price first
                        {"price": 5.0, "size": 300.0},
                        {"price": 4.8, "size": 500.0}
                    ],
                    "availableToLay": [
                        {"price": 5.4, "size": 80.0},    # best lay price first
                        {"price": 5.6, "size": 120.0}
                    ]
                }
            },
            ...
        ]
    }

    Back vs Lay prices
    ------------------
    - availableToBack : what punters are willing to LAY (what you can BACK at).
                        Sorted descending — highest price (best for backer) first.
    - availableToLay  : what punters want to BACK (what you can LAY at).
                        Sorted ascending — lowest price (best for layer) first.

    Returns
    -------
    dict
        The market book for the requested market (first element of the API response list).

    Raises
    ------
    ValueError
        If the API returns an empty list for the given market ID.
    requests.HTTPError
        If the Betfair API returns a non-2xx status code.
    """
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

    result = betfair_post("listMarketBook/", payload)

    if not result:
        raise ValueError(f"No market book returned for market {market_id}")

    return result[0]
