from backend.services.betfair_client import betfair_post, list_events, list_market_catalogue
from backend.services.market_resolver import resolve_selection
from backend.config.sport_mapping import SPORT_EVENT_TYPE_MAP, MARKET_TYPE_OVERRIDES


def find_all_events_for_sport(sport: str, session: dict) -> list:
    event_type_id = SPORT_EVENT_TYPE_MAP.get(sport.lower())
    if not event_type_id:
        raise ValueError(f"Unsupported sport: {sport}")
    return betfair_post("listEvents/", {"filter": {"eventTypeIds": [event_type_id]}}, session)


def find_event_candidates(parsed_bet, session: dict) -> list:
    sport = parsed_bet.sport.lower()
    event_type_id = SPORT_EVENT_TYPE_MAP.get(sport)

    if not event_type_id:
        raise ValueError(f"Unsupported sport: {sport}")

    return list_events(parsed_bet.selection_name, event_type_id, session)


def resolve_market(event_id: str, parsed_bet, session: dict) -> dict:
    sport = parsed_bet.sport.lower()
    overrides = MARKET_TYPE_OVERRIDES.get(sport, {})
    market_type = overrides.get(parsed_bet.market_type, parsed_bet.market_type)

    markets = list_market_catalogue(event_id, market_type, session)

    if not markets:
        raise ValueError(f"No {market_type} market found for event {event_id}")

    market = markets[0] # TODO: Not sure if this is required instead can just return first market
    selection_id = resolve_selection(market["runners"], parsed_bet.selection_name)

    if selection_id is None:
        raise ValueError(
            f"Runner '{parsed_bet.selection_name}' not found in market {market['marketId']}."
        )

    return {
        "eventId": event_id,
        "marketId": market["marketId"],
        "selectionId": selection_id,
        "competition": market.get("competition", {}).get("name"),
    }


def get_upcoming_fixtures(team_name: str, sport: str, session: dict, limit: int = 3) -> list:
    event_type_id = SPORT_EVENT_TYPE_MAP.get(sport.lower())
    if not event_type_id:
        return []

    try:
        events = list_events(team_name, event_type_id, session)
    except Exception:
        return []

    fixtures = []
    for ev in events[:limit]:
        e = ev.get("event", {})
        name = e.get("name", "")
        date = e.get("openDate", None)
        event_id = e.get("id")

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
