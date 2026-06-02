from backend.services.betfair_client import list_events, list_market_catalogue
from backend.services.market_resolver import resolve_selection
from backend.config.sport_mapping import SPORT_EVENT_TYPE_MAP


def find_event_candidates(parsed_bet, session: dict) -> list:
    """
    Search Betfair for events matching the selection name and sport.

    Returns the raw list of event dicts from listEvents — every element has
    the shape {"event": {"id", "name", "openDate", ...}, "marketCount": int}.
    Returns an empty list if Betfair finds nothing; does not raise.

    The caller is responsible for passing this list to AIInterpreter.select_event
    to pick the correct event rather than blindly taking index 0.
    """
    sport = parsed_bet.sport.lower()
    event_type_id = SPORT_EVENT_TYPE_MAP.get(sport)

    if not event_type_id:
        raise ValueError(f"Unsupported sport: {sport}")

    return list_events(parsed_bet.selection_name, event_type_id, session)


def resolve_market(event_id: str, parsed_bet, session: dict) -> dict:
    """
    Given a confirmed event ID, fetch the market and resolve the runner.

    Calls listMarketCatalogue to get markets for the event, then matches
    the selection name against the runner list to get the selectionId.

    Returns
    -------
    dict with keys: eventId, marketId, selectionId (all strings)

    Raises
    ------
    ValueError
        If no markets are found for the event, or the named runner is not
        present in the market (e.g. name mismatch after AI extraction).
    """
    markets = list_market_catalogue(event_id, parsed_bet.market_type, session)

    if not markets:
        raise ValueError(f"No {parsed_bet.market_type} market found for event {event_id}")

    market = markets[0]
    selection_id = resolve_selection(market["runners"], parsed_bet.selection_name)

    if selection_id is None:
        raise ValueError(
            f"Runner '{parsed_bet.selection_name}' not found in market {market['marketId']}. "
            "The selection name from the AI may not match the Betfair runner name exactly."
        )

    return {
        "eventId": event_id,
        "marketId": market["marketId"],
        "selectionId": selection_id,
        "competition": market.get("competition", {}).get("name"),
    }
