from backend.services.betfair_client import list_events, list_market_catalogue, get_best_price
from backend.services.market_resolver import resolve_selection
from backend.config.sport_mapping import SPORT_EVENT_TYPE_MAP


def search_market(parsed_bet):

    sport = parsed_bet.sport.lower()
    event_type_id = SPORT_EVENT_TYPE_MAP.get(sport)

    if not event_type_id:
        raise ValueError(f"Unsupported sport: {sport}")

    events = list_events(parsed_bet.selection_name, event_type_id)
    if not events:
        raise ValueError(f"No events found for {parsed_bet.selection_name}")

    event = events[0]["event"]
    event_id = event["id"]

    # Extract event name and date
    event_name = event.get("name", "")         # e.g. "Chelsea v Man City"
    event_date = event.get("openDate", None)   # ISO datetime string

    markets = list_market_catalogue(event_id, parsed_bet.market_type)
    market = markets[0]
    runners = market["runners"]

    selection_id = resolve_selection(runners, parsed_bet.selection_name)
    live_price = get_best_price(market["marketId"], selection_id, parsed_bet.side)

    # Parse opponent from event name (e.g. "Chelsea v Man City" → opponent is "Chelsea")
    opponent = None
    if event_name:
        parts = [p.strip() for p in event_name.replace(" vs ", " v ").split(" v ")]
        if len(parts) == 2:
            # Opponent is whichever side isn't the selection
            sel = parsed_bet.selection_name.lower()
            for part in parts:
                if sel not in part.lower():
                    opponent = part
                    break

    return {
        "eventId": event_id,
        "eventName": event_name,
        "eventDate": event_date,
        "opponent": opponent,
        "marketId": market["marketId"],
        "selectionId": selection_id,
        "livePrice": live_price
    }