import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from backend.services.betfair_auth import get_token, clear_token, SessionExpiredError

load_dotenv()

BETFAIR_ENDPOINT = "https://api.betfair.com/exchange/betting/rest/v1.0/"

# Betfair weights listMarketBook requests: sum(weight) * num_markets <= 200, and
# EX_BEST_OFFERS has weight 5, so at most 40 markets can be priced per call.
MARKET_BOOK_BATCH_SIZE = 40

# (connect, read) seconds. Without this, requests waits forever, so a slow or hung
# Betfair response blocks the whole request indefinitely (the OpenAI client already
# caps at 30s). The read bound covers the heavy listMarketBook calls.
BETFAIR_TIMEOUT = (5, 15)


def betfair_post(path: str, payload: dict, session: dict):
    headers = {
        "X-Application": os.getenv("BETFAIR_APP_KEY"),
        "X-Authentication": get_token(session),
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(
            BETFAIR_ENDPOINT + path, json=payload, headers=headers,
            timeout=BETFAIR_TIMEOUT,
        )
    except requests.Timeout:
        raise ValueError(f"Betfair request to {path} timed out — please try again")

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


def list_events_filtered(
    event_type_id: str,
    session: dict,
    text_query: str | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
    countries: list[str] | None = None,
) -> list:
    """Events for a sport, optionally narrowed by a text query, a market
    start-time window, and/or a list of countries. Used by the natural-language
    search path, where the agent supplies a competition/team text and a date
    window (e.g. "this evening").

    `text_query` matches competition names as well as event names (a Betfair
    quirk), which is what we want for competition-level queries like "World Cup".
    It does NOT match location/country — those must go through `countries`.
    `countries` are ISO 3166-1 alpha-2 codes (e.g. ["AU"], ["GB", "IE"]) and map
    to Betfair's `marketCountries` filter — the right way to scope e.g. Australian
    greyhound meetings, whose names ("Wentworth Park") never contain "Australia".
    `time_from`/`time_to` are ISO 8601 strings (e.g. "2026-06-17T17:00:00Z").
    """
    filter_: dict = {"eventTypeIds": [event_type_id]}
    if text_query:
        filter_["textQuery"] = text_query
    if countries:
        filter_["marketCountries"] = countries
    if time_from or time_to:
        window = {}
        if time_from:
            window["from"] = time_from
        if time_to:
            window["to"] = time_to
        filter_["marketStartTime"] = window
    return betfair_post("listEvents/", {"filter": filter_}, session)


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


def list_markets_for_events(
    event_ids: list[str],
    session: dict,
    market_type_codes: list[str] | None = None,
    max_results: int = 100,
) -> list:
    """Markets across a set of events, ranked by liquidity (most money matched
    first). Cheap structural call — no prices. `totalMatched` is returned at the
    top level of each catalogue item, so callers can rank and cap by liquidity
    before paying for any market book. Optionally filtered to specific market
    types (e.g. the OVER_UNDER family for an over/under query).
    """
    if not event_ids:
        return []
    filter_: dict = {"eventIds": event_ids}
    if market_type_codes:
        filter_["marketTypeCodes"] = market_type_codes
    payload = {
        "filter": filter_,
        "maxResults": str(max_results),
        # MARKET_DESCRIPTION carries description.marketType, which is how a
        # market's type code is read back from the catalogue (see racing_service).
        "marketProjection": [
            "RUNNER_DESCRIPTION", "EVENT", "COMPETITION",
            "MARKET_START_TIME", "MARKET_DESCRIPTION",
        ],
        "sort": "MAXIMUM_TRADED",
    }
    return betfair_post("listMarketCatalogue/", payload, session)


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


# Outright / competition-level market types across every sport (see docs/Market_Types.md).
# For ALL of these the EVENT is a competition ("FIFA World Cup", "The Open") — not a
# single fixture — and the team/player is a RUNNER, so textQuery can't find them; we
# bulk-fetch and scan runners (the racing pattern, see list_racing_markets /
# racing_service._scan_for_runner). This is the BROAD set: tournament winners plus
# the adjacent competition markets where the participant is also a runner — placings
# (top-5/10/20, make the cut), group stage, qualification/progression, top
# nationality/region, scorer & player awards, and tournament head-to-head match
# bets. Per-FIXTURE types (MATCH_ODDS, OVER_UNDER*, CORRECT_SCORE, HANDICAP, …) are
# deliberately EXCLUDED: a participant's own matches are already covered by the
# find_events fixture path, and those types dwarf the outrights by volume (so they
# would crowd the real outrights out of any liquidity-capped fetch).
OUTRIGHT_MARKET_TYPES = [
    # tournament / competition winners
    "WINNER", "OUTRIGHT_WINNER", "TOURNAMENT_WINNER", "SERIES_WINNER",
    "CHAMPIONSHIP_WINNER", "CONSTRUCTORS_WINNER", "SUPER_BOWL_WINNER",
    "FIRST_WINNER", "WINNING_REGION", "NAME_THE_FINALISTS",
    # progression / elimination / qualification
    "TO_REACH_FINAL", "TO_REACH_SEMIS", "TO_REACH_QUARTERS", "TO_REACH_ROUND_16",
    "TO_QUALIFY", "STAGE_OF_ELIMINATION",
    # group-stage outrights
    "GROUP_A_WINNER", "GROUP_B_WINNER", "GROUP_C_WINNER", "GROUP_D_WINNER",
    "GROUP_E_WINNER", "GROUP_F_WINNER", "GROUP_G_WINNER", "GROUP_H_WINNER",
    "GROUP_I_WINNER", "GROUP_J_WINNER", "GROUP_K_WINNER", "GROUP_L_WINNER",
    "GROUP_A_WINNER_SGX",
    "GROUP_A_TO_QUALIFY", "GROUP_B_TO_QUALIFY", "GROUP_C_TO_QUALIFY",
    "GROUP_D_TO_QUALIFY", "GROUP_E_TO_QUALIFY", "GROUP_F_TO_QUALIFY",
    "GROUP_G_TO_QUALIFY", "GROUP_H_TO_QUALIFY", "GROUP_I_TO_QUALIFY",
    "GROUP_J_TO_QUALIFY", "GROUP_K_TO_QUALIFY", "GROUP_L_TO_QUALIFY",
    # placings / finishing position (golf especially; league top-N)
    "TOP_N_FINISH", "TOP_5_FINISH", "TOP_8_FINISH", "TOP_10_FINISH",
    "TOP_20_FINISH", "MAKE_THE_CUT", "EACH_WAY", "ROUND_LEADER",
    "TOP_4", "TOP_6", "TOP_10",
    # top nationality / region / category
    "TOP_NATIONALITY", "TOP_ENGLISH_PLAYER", "TOP_IRISH_PLAYER", "TOP_EUROPEAN",
    "TOP_CONTINENTAL", "TOP_USA", "TOP_ASIAN",
    "TOP_EURO_TEAM", "TOP_CONCACAF_TEAM", "TOP_CONMEBOL_TEAM", "TOP_AFC_TEAM",
    "TOP_CAF_TEAM",
    # awards / scorers
    "GOLDEN_BOOT", "WINNER_GOLDEN_BOOT", "GOLDEN_GLOVE", "TOP_GOALSCORER",
    "TOP_SCORING_CLUB", "YOUNG_PLAYER",
    # tournament head-to-head match bets
    "MATCH_BET", "TOUR_MATCHBET_NTIE1",
]


def list_outright_markets(event_type_id: str, session: dict, max_results: int = 500) -> list:
    """All upcoming outright / competition-level markets for a sport in one call.

    Fetches every market whose type is in OUTRIGHT_MARKET_TYPES — the markets where
    a team/player is a RUNNER rather than named in the event ("England" in "FIFA
    World Cup", "Scottie Scheffler" in "The Open") — so the search agent can surface
    "X to win / place / reach the final / …" by scanning runners, the same way
    racing finds a horse without knowing the meeting. These types are individually
    rare, so even the broad allowlist returns a small pool. No marketStartTime
    filter: an in-progress tournament's winner market has a past start time but is
    still open, and listMarketCatalogue already omits settled/closed markets.
    Liquidity-ranked so the busiest competitions (World Cup, the majors) come first.
    """
    payload = {
        "filter": {
            "eventTypeIds": [event_type_id],
            "marketTypeCodes": OUTRIGHT_MARKET_TYPES,
        },
        "maxResults": str(max_results),
        "marketProjection": [
            "RUNNER_DESCRIPTION", "EVENT", "COMPETITION",
            "MARKET_START_TIME", "MARKET_DESCRIPTION",
        ],
        "sort": "MAXIMUM_TRADED",
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


def list_market_books(market_ids: list[str], session: dict) -> list:
    """Market books for many markets at once, batched to respect Betfair's
    data-request weight limit (EX_BEST_OFFERS = weight 5, cap 200 → 40 markets
    per call). Chunks the ids, issues one listMarketBook per chunk, and returns
    the concatenated book dicts (market status + runners with ex prices) — same
    projection as get_market_book, so callers read best back/lay + size the same
    way. The search pricing tool layers a budget on top of this.
    """
    if not market_ids:
        return []
    books = []
    for i in range(0, len(market_ids), MARKET_BOOK_BATCH_SIZE):
        chunk = market_ids[i:i + MARKET_BOOK_BATCH_SIZE]
        payload = {
            "marketIds": chunk,
            "priceProjection": {
                "priceData": ["EX_BEST_OFFERS"],
                "exBestOffersOverrides": {
                    "bestPricesDepth": 3,
                    "rollupModel": "STAKE",
                    "maximumRollup": 10,
                },
            },
        }
        books.extend(betfair_post("listMarketBook/", payload, session))
    return books


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
