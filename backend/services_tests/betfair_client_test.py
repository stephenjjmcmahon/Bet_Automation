import sys
import os

# Allow Python to find the services folder
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.betfair_client import list_events, list_market_catalogue
from services.market_resolver import resolve_selection

print("TEST SCRIPT STARTED")

# Step 1 — Search events containing "Arsenal"
events = list_events("Arsenal")

print("\nEvents returned from Betfair:")
print(events)

if not events:
    print("No events found.")
    exit()

# Step 2 — Take the first event
event_id = events[0]["event"]["id"]
event_name = events[0]["event"]["name"]

print("\nUsing event:")
print(event_name)
print("Event ID:", event_id)

# Step 3 — Get markets for that event
markets = list_market_catalogue(event_id)

print("\nMarkets returned from Betfair:")
print(markets)

# Step 4: Extract runners
runners = markets[0]["runners"]

# Step 5: Resolve selection
selection_id = resolve_selection(runners, "Arsenal")

print("\nResolved selectionId:")
print(selection_id)

print("\nTEST COMPLETE")