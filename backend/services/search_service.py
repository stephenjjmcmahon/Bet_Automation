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

    event = events[0]["event"] # Takes the first event, which is only based on name and sport
    event_id = event["id"]
    event_name = event.get("name", "")
    event_date = event.get("openDate", None)

    markets = list_market_catalogue(event_id, parsed_bet.market_type)
    market = markets[0]
    runners = market["runners"]

    selection_id = resolve_selection(runners, parsed_bet.selection_name)
    live_price = get_best_price(market["marketId"], selection_id, parsed_bet.side)

    opponent = None
    if event_name:
        parts = [p.strip() for p in event_name.replace(" vs ", " v ").split(" v ")]
        if len(parts) == 2:
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


def get_upcoming_fixtures(team_name: str, sport: str = "football", limit: int = 3): # Football hardcoded
    """Return next N upcoming fixtures for a team — used for game picker UI."""
    event_type_id = SPORT_EVENT_TYPE_MAP.get(sport.lower())
    if not event_type_id:
        return []

    try:
        events = list_events(team_name, event_type_id)
    except Exception:
        return []

    fixtures = []
    for ev in events[:limit]:
        e = ev.get("event", {})
        name = e.get("name", "")
        date = e.get("openDate", None)
        event_id = e.get("id")

        # Parse opponent from event name
        opponent = None
        parts = [p.strip() for p in name.replace(" vs ", " v ").split(" v ")]
        if len(parts) == 2:
            sel = team_name.lower()
            for part in parts:
                if sel not in part.lower():
                    opponent = part
                    break

        fixtures.append({
            "eventId": event_id,
            "eventName": name,
            "eventDate": date,
            "opponent": opponent or name,
        })

    return fixtures 
