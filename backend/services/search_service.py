from backend.services.betfair_client import betfair_post, list_events, list_market_catalogue, list_market_types_for_events, list_market_types_for_sport
from backend.services.market_resolver import resolve_selection
from backend.config.sport_mapping import SPORT_EVENT_TYPE_MAP, COMPETITION_SPORTS


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
    query = parsed_bet.event_name or parsed_bet.selection_name
    return list_events(query, event_type_id, session)


def get_market_types(sport: str, candidates: list, session: dict) -> list[str]:
    """One API call: event IDs batch for H2H sports, sport-level for competition sports."""
    if sport.lower() in COMPETITION_SPORTS:
        event_type_id = SPORT_EVENT_TYPE_MAP.get(sport.lower())
        if not event_type_id:
            return []
        return list_market_types_for_sport(event_type_id, session)
    else:
        event_ids = [c["event"]["id"] for c in candidates]
        if not event_ids:
            return []
        return list_market_types_for_events(event_ids, session)


def resolve_market(event_id: str, parsed_bet, session: dict, market_type: str) -> dict:
    markets = list_market_catalogue(event_id, market_type, session)

    print(f"DEBUG resolve_market — event={event_id} type={market_type} ({len(markets)} markets found)")
    print()

    if not markets:
        raise ValueError(f"No {market_type} market found for event {event_id}")

    market = markets[0]
    print(f"  DEBUG market runners (first 6): {[(r.get('runnerName'), r.get('handicap'), r.get('selectionId')) for r in market.get('runners', [])[:6]]}")

    selection_id, runner_name = resolve_selection(market["runners"], parsed_bet.selection_name)
    print(f"  DEBUG resolved selectionId={selection_id}  (looking for name='{parsed_bet.selection_name}' line={parsed_bet.line})")
    print()

    if selection_id is None:
        raise ValueError(
            f"Runner '{parsed_bet.selection_name}' not found in market {market['marketId']}."
        )

    return {
        "eventId": event_id,
        "marketId": market["marketId"],
        "selectionId": selection_id,
        "runnerName": runner_name,
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
