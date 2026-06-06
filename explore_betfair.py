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
SPORT = "greyhound racing"   # change to any key in SPORT_EVENT_TYPE_MAP
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

event_type_id = SPORT_EVENT_TYPE_MAP.get(SPORT)
if not event_type_id:
    print(f"Unknown sport '{SPORT}'. Available: {list(SPORT_EVENT_TYPE_MAP.keys())}")
    sys.exit(1)

filter_ = {"eventTypeIds": [event_type_id]}

# Market types
market_types = betfair_post("listMarketTypes/", {"filter": filter_}, session)
print(f"=== Market types for {SPORT} ({len(market_types)} found) ===")
#for mt in sorted(market_types, key=lambda x: x.get("marketCount", 0), reverse=True):
 #   print(f"  {mt['marketType']:<35}  {mt.get('marketCount', '?')} markets")

#print()

# Competitions
competitions = betfair_post("listCompetitions/", {"filter": filter_}, session)
print(f"=== Competitions for {SPORT} ({len(competitions)} found) ===")
for c in sorted(competitions, key=lambda x: x.get("marketCount", 0), reverse=True):
    name = c.get("competition", {}).get("name", "?")
    region = c.get("competitionRegion", "")
    print(f"  {name:<40}  {region:<20}  {c.get('marketCount', '?')} markets")

print()

# Events (first 20)
events = betfair_post("listEvents/", {"filter": filter_}, session)
print(f"=== Events for {SPORT} ({len(events)} found, showing first 20) ===")
for ev in events[:20]:
    e = ev.get("event", {})
    print(f"  {e.get('name', '?'):<50}  {e.get('openDate', '?')[:10]}  id={e.get('id')}")
