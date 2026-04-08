from backend.services.betfair_client import list_events, list_market_catalogue
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

    event_id = events[0]["event"]["id"]

    markets = list_market_catalogue(
        event_id,
        parsed_bet.market_type
    )

    market = markets[0]

    runners = market["runners"]

    selection_id = resolve_selection(
        runners,
        parsed_bet.selection_name
    )
    #print("Parsed bet:", parsed_bet)
   # print("Sport:", parsed_bet.sport)
    #print("EventTypeId:", event_type_id)

    return {
        "eventId": event_id,
        "marketId": market["marketId"],
        "selectionId": selection_id
    }