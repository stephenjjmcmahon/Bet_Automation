"""
Developer script — discovery aid, not part of the app.

Run from the repo root:  python -m scripts.explore_betfair
Prints all events and market types available on Betfair for a given sport.
Reads credentials from .env — make sure BETFAIR_USERNAME/PASSWORD/APP_KEY are set.
"""

import os
import sys

from dotenv import load_dotenv

from backend.config.sport_mapping import SPORT_EVENT_TYPE_MAP
from backend.services.betfair_auth import login
from backend.services.betfair_client import betfair_post

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
SPORT = "rugby union"   # change to any key in SPORT_EVENT_TYPE_MAP
# ─────────────────────────────────────────────────────────────────────────────


def list_market_types_for_sport(sport: str, session: dict) -> list:
    """Print every market type Betfair currently exposes for `sport`, busiest first.

    Returns the raw list so callers can reuse it. `sport` must be a key in
    SPORT_EVENT_TYPE_MAP (case-insensitive).
    """
    event_type_id = SPORT_EVENT_TYPE_MAP.get(sport.lower())
    if not event_type_id:
        print(f"Unknown sport '{sport}'. Available: {list(SPORT_EVENT_TYPE_MAP.keys())}")
        return []

    market_types = betfair_post(
        "listMarketTypes/", {"filter": {"eventTypeIds": [event_type_id]}}, session
    )
    print(f"=== Market types for {sport} ({len(market_types)} found) ===")
    for mt in sorted(market_types, key=lambda x: x.get("marketCount", 0), reverse=True):
        print(f"  {mt['marketType']:<35}  {mt.get('marketCount', '?')} markets")
    print()
    return market_types


def list_market_types_for_all_sports(session: dict) -> None:
    """Print every market type for every sport in SPORT_EVENT_TYPE_MAP."""
    print("########## Market types for ALL sports ##########\n")
    for sport in SPORT_EVENT_TYPE_MAP:
        list_market_types_for_sport(sport, session)

username = os.getenv("BETFAIR_USERNAME") or input("Betfair username: ")
password = os.getenv("BETFAIR_PASSWORD") or input("Betfair password: ")

session = {}
try:
    login(username, password, session)
    print("Logged in.\n")
except Exception as e:
    print(f"Login failed: {e}")
    sys.exit(1)

# Every market type for every sport.
list_market_types_for_all_sports(session)

event_type_id = SPORT_EVENT_TYPE_MAP.get(SPORT)
if not event_type_id:
    print(f"Unknown sport '{SPORT}'. Available: {list(SPORT_EVENT_TYPE_MAP.keys())}")
    sys.exit(1)

filter_ = {"eventTypeIds": [event_type_id]}

# Market types
market_types = list_market_types_for_sport(SPORT, session)

# Events
events = betfair_post("listEvents/", {"filter": filter_}, session)
print(f"=== Events for {SPORT} ({len(events)} found) ===")
for ev in events:
    e = ev.get("event", {})
    print(f"  {e.get('name', '?'):<50}  {e.get('openDate', '?')[:10]}  id={e.get('id')}")
print()

# For horse racing a meeting is an *event*; the individual races are WIN markets
# beneath it. Drill into the first five meetings to show their races and runners.
if SPORT in ("horse racing", "greyhound racing"):
    print("=== Races (WIN markets) and runners — first 5 meetings ===")
    for ev in events[:5]:
        e = ev.get("event", {})
        markets = betfair_post(
            "listMarketCatalogue/",
            {
                "filter": {"eventIds": [e.get("id")], "marketTypeCodes": ["WIN"]},
                "maxResults": "20",
                "marketProjection": ["MARKET_START_TIME", "RUNNER_DESCRIPTION"],
                "sort": "FIRST_TO_START",
            },
            session,
        )
        print(f"\n{e.get('name', '?')}  (id={e.get('id')})  —  {len(markets)} races")
        for m in markets:
            start = (m.get("marketStartTime") or "?")[11:16]
            print(f"  {start}  {m.get('marketName', '?'):<22}  marketId={m['marketId']}")
            for r in m.get("runners", []):
                print(f"      {r.get('runnerName', '?'):<30}  selectionId={r.get('selectionId')}")

# A single race can have several PLACE markets (To Be Placed, Top 2/3/4), all of
# market type PLACE, differing only by numberOfWinners — which lives on the
# market BOOK, not the catalogue. Probe the first 3 meetings to see how the
# variants actually appear: names, market type (PLACE vs OTHER_PLACE), how many
# per race, and the number of places each pays.
if SPORT in ("horse racing", "greyhound racing"):
    print("\n=== Place markets per race — first 3 meetings ===")
    for ev in events[:3]:
        e = ev.get("event", {})
        place_markets = betfair_post(
            "listMarketCatalogue/",
            {
                "filter": {"eventIds": [e.get("id")], "marketTypeCodes": ["PLACE", "OTHER_PLACE"]},
                "maxResults": "50",
                "marketProjection": ["MARKET_START_TIME", "RUNNER_DESCRIPTION", "MARKET_DESCRIPTION"],
                "sort": "FIRST_TO_START",
            },
            session,
        )

        # numberOfWinners (places paid) is only on the book — fetch in one call.
        market_ids = [m["marketId"] for m in place_markets]
        winners_by_id = {}
        if market_ids:
            books = betfair_post("listMarketBook/", {"marketIds": market_ids}, session)
            winners_by_id = {b["marketId"]: b.get("numberOfWinners") for b in books}

        print(f"\n{e.get('name', '?')}  —  {len(place_markets)} place markets")
        for m in place_markets:
            start = (m.get("marketStartTime") or "?")[11:16]
            mtype = m.get("description", {}).get("marketType", "?")
            places = winners_by_id.get(m["marketId"], "?")
            print(f"  {start}  type={mtype:<12} name='{m.get('marketName', '?')}'  "
                  f"numberOfWinners={places}  runners={len(m.get('runners', []))}  "
                  f"marketId={m['marketId']}")
