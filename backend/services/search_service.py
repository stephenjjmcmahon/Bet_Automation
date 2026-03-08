from services.betfair_client import list_events, list_market_catalogue
from services.market_resolver import resolve_selection


def search_market(parsed_bet):

    events = list_events(parsed_bet.selection_name)

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

    return {
        "eventId": event_id,
        "marketId": market["marketId"],
        "selectionId": selection_id
    }