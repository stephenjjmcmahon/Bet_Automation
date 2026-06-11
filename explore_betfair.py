"""
Run with:  python explore_betfair.py
Prints all market types and competitions available on Betfair for a given sport.
Reads credentials from .env — make sure BETFAIR_USERNAME/PASSWORD/APP_KEY are set.
"""

import json
import os
import sys
from dotenv import load_dotenv
from backend.services.betfair_auth import login
from backend.services.betfair_client import betfair_post
from backend.config.sport_mapping import SPORT_EVENT_TYPE_MAP

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
SPORT = "basketball"   # change to any key in SPORT_EVENT_TYPE_MAP
OUTRIGHT_QUERY = "World Cup"  # competition name to probe for outright markets; None to skip
OUTRIGHT_SPORT = "football"
# ─────────────────────────────────────────────────────────────────────────────

username = os.getenv("BETFAIR_USERNAME") or input("Betfair username: ")
password = os.getenv("BETFAIR_PASSWORD") or input("Betfair password: ")

session = {}
try:
    login(username, password, session)
    print("Logged in.\n")
except Exception as e:
    print(f"Login failed: {e}")
    sys.exit(1)

# ── Outright (competition winner) market exploration ─────────────────────────
if OUTRIGHT_QUERY:
    outright_type_id = SPORT_EVENT_TYPE_MAP[OUTRIGHT_SPORT]

    # What does the app's current event search path return for the competition name?
    events = betfair_post(
        "listEvents/",
        {"filter": {"eventTypeIds": [outright_type_id], "textQuery": OUTRIGHT_QUERY}},
        session,
    )
    non_h2h = [ev for ev in events if " v " not in ev["event"]["name"]]
    print(f"=== listEvents textQuery='{OUTRIGHT_QUERY}': {len(events)} events, "
          f"{len(non_h2h)} non-H2H ===")
    for ev in non_h2h:
        e = ev["event"]
        print(f"  {e['name']:<55}  id={e['id']}  openDate={e.get('openDate')}")
    print()

    # Find outright markets directly and show their event + runners
    catalogue = betfair_post(
        "listMarketCatalogue/",
        {
            "filter": {
                "eventTypeIds": [outright_type_id],
                "textQuery": OUTRIGHT_QUERY,
                "marketTypeCodes": ["WINNER", "OUTRIGHT_WINNER", "TOURNAMENT_WINNER"],
            },
            "maxResults": "10",
            "marketProjection": ["EVENT", "MARKET_DESCRIPTION", "RUNNER_DESCRIPTION"],
        },
        session,
    )
    print(f"=== Outright markets matching '{OUTRIGHT_QUERY}' ({len(catalogue)}) ===")
    for m in catalogue:
        mtype = m.get("description", {}).get("marketType", "?")
        print(f"  '{m.get('marketName')}' (type={mtype}) on event "
              f"'{m.get('event', {}).get('name')}' marketId={m['marketId']}")
        for r in m.get("runners", [])[:10]:
            print(f"      {r.get('runnerName'):<35}  selectionId={r.get('selectionId')}")
    print()

from datetime import datetime, timezone
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
all_events = betfair_post("listEvents/", {"filter": {"marketStartTime": {"from": now}}}, session)
print(f"Upcoming events on Betfair: {len(all_events)}")

all_market_types = betfair_post("listMarketTypes/", {"filter": {}}, session)
print(f"=== All market types on exchange ({len(all_market_types)} total) ===")
for mt in sorted(all_market_types, key=lambda x: x.get("marketCount", 0), reverse=True):
    print(f"  {mt['marketType']:<40}  {mt.get('marketCount', '?')} markets")
print()

line_market_types = [mt for mt in all_market_types if mt["marketType"].endswith("_LINE")]
print(f"=== Line market types on exchange ({len(line_market_types)} total) ===")
for mt in sorted(line_market_types, key=lambda x: x.get("marketCount", 0), reverse=True):
    print(f"  {mt['marketType']:<40}  {mt.get('marketCount', '?')} markets")
print()

event_type_id = SPORT_EVENT_TYPE_MAP.get(SPORT)
if not event_type_id:
    print(f"Unknown sport '{SPORT}'. Available: {list(SPORT_EVENT_TYPE_MAP.keys())}")
    sys.exit(1)

filter_ = {"eventTypeIds": [event_type_id]}

# Market types
market_types = betfair_post("listMarketTypes/", {"filter": filter_}, session)
print(f"=== Market types for {SPORT} ({len(market_types)} found) ===")
for mt in sorted(market_types, key=lambda x: x.get("marketCount", 0), reverse=True):
    print(f"  {mt['marketType']:<35}  {mt.get('marketCount', '?')} markets")

print()

# Competitions
competitions = betfair_post("listCompetitions/", {"filter": filter_}, session)
print(f"=== Competitions for {SPORT} ({len(competitions)} found) ===")
#for c in sorted(competitions, key=lambda x: x.get("marketCount", 0), reverse=True):
 #   name = c.get("competition", {}).get("name", "?")
  #  region = c.get("competitionRegion", "")
   # print(f"  {name:<40}  {region:<20}  {c.get('marketCount', '?')} markets")

#print()

# Events (first 20)
events = betfair_post("listEvents/", {"filter": filter_}, session)
print(f"=== Events for {SPORT} ({len(events)} found, showing first 20) ===")
for ev in events[:20]:
    e = ev.get("event", {})
    print(f"  {e.get('name', '?'):<50}  {e.get('openDate', '?')[:10]}  id={e.get('id')}")

# ── All sports and their markets ──────────────────────────────────────────────
print("\n" + "="*70)
print("=== ALL SPORTS AND THEIR MARKET TYPES ===")
print("="*70)

# Deduplicate by event_type_id so aliases (e.g. soccer/football) aren't fetched twice
seen_ids = {}
for sport_name, event_type_id in SPORT_EVENT_TYPE_MAP.items():
    if event_type_id not in seen_ids:
        seen_ids[event_type_id] = sport_name

for event_type_id, sport_name in sorted(seen_ids.items(), key=lambda x: x[1]):
    try:
        market_types = betfair_post(
            "listMarketTypes/",
            {"filter": {"eventTypeIds": [event_type_id]}},
            session,
        )
        market_names = sorted(mt["marketType"] for mt in market_types)
        print(f"\n{sport_name.upper()} (id={event_type_id})  —  {len(market_names)} market types")
        for name in market_names:
            count = next((mt.get("marketCount", "?") for mt in market_types if mt["marketType"] == name), "?")
            print(f"    {name:<45}  {count} markets")
    except Exception as e:
        print(f"\n{sport_name.upper()} (id={event_type_id})  —  ERROR: {e}")
