from backend.services.betfair_client import betfair_post, list_events, list_market_catalogue, list_market_types_for_events, list_market_types_for_sport, get_market_book
from backend.services.market_resolver import resolve_selection
from backend.services.ai_interpreter import AIInterpreter
from backend.config.sport_mapping import SPORT_EVENT_TYPE_MAP, COMPETITION_SPORTS, event_type_id_for

# Max events to hand the AI in the zero-result fallback. Small sports (basketball
# and below, ≤35) fit comfortably; cricket (51) / tennis (282) / soccer (332) are
# too big a haystack for reliable ranking, so for those we 404 and ask the user
# to add an opponent/competition instead. Gated on actual count rather than a
# sport list because totals swing seasonally.
FALLBACK_MAX_EVENTS = 40


def find_all_events_for_sport(sport: str, session: dict) -> list:
    event_type_id = event_type_id_for(sport)
    return betfair_post("listEvents/", {"filter": {"eventTypeIds": [event_type_id]}}, session)


def find_event_candidates(parsed_bet, session: dict) -> list:
    event_type_id = event_type_id_for(parsed_bet.sport)
    query = parsed_bet.event_name or parsed_bet.selection_name
    events = list_events(query, event_type_id, session)

    # Fallback: a text search that matches nothing (garbled/unusual name, or an
    # outright whose competition the parser named differently from Betfair, e.g.
    # "Premiership" vs Betfair's "AFL") would otherwise dead-end in a 404. Fetch
    # every event for the sport and let the AI scan them — but only if the list
    # is small enough to rank reliably (see FALLBACK_MAX_EVENTS); otherwise fall
    # through to the empty result, which surfaces the existing 404.
    if not events:
        all_events = find_all_events_for_sport(parsed_bet.sport, session)
        if len(all_events) <= FALLBACK_MAX_EVENTS:
            return all_events

    # Outright bets target a competition-level event (e.g. "FIFA World Cup").
    # Betfair's textQuery also matches competition names, so it returns every
    # fixture in the competition too — keep only events whose own name matches
    # the query so the AI ranks outright events, not fixtures.
    if parsed_bet.market_type == "OUTRIGHT_WINNER":
        matched = [ev for ev in events if query.lower() in ev["event"]["name"].lower()]
        if matched:
            return matched

    return events


def get_market_types(sport: str, candidates: list, session: dict) -> list[str]:
    """One API call: event IDs batch for H2H sports, sport-level for competition sports."""
    if sport.lower() in COMPETITION_SPORTS:
        # Sport is already validated by find_event_candidates/find_all_events_for_sport
        # upstream, so an unmapped sport raises rather than silently returning [].
        return list_market_types_for_sport(event_type_id_for(sport), session)
    else:
        event_ids = [c["event"]["id"] for c in candidates]
        if not event_ids:
            return []
        return list_market_types_for_events(event_ids, session)


def _line_token(line) -> str:
    """The handicap line as it appears inside a market/runner name ('39.5')."""
    return ("%g" % line) if line is not None else ""


def _match_by_name(markets: list, parsed_bet) -> tuple:
    """Find a runner whose catalogue name matches the bet, scanning every market
    of the type (not just the first) so alternate-line markets are reachable.

    Returns (market, selection_id, runner_name) or (None, None, None). When a
    line is given and several markets match, prefers the market whose name or
    matched runner encodes that line (e.g. "Winning Margin 39.5").
    """
    hits = []
    for m in markets:
        sid, name = resolve_selection(m.get("runners", []), parsed_bet.selection_name)
        if sid is not None:
            hits.append((m, sid, name or ""))
    if not hits:
        return None, None, None

    token = _line_token(parsed_bet.line)
    if token:
        for m, sid, name in hits:
            if token in f"{m.get('marketName', '')} {name}":
                return m, sid, name
    return hits[0]


def _match_by_ai(markets: list, user_input: str) -> tuple:
    """AI runner-name fallback over the runners of EVERY market of the type at
    once, so the pick also chooses which market (e.g. which WINNING_MARGIN line).

    Returns (market, selection_id, runner_name) or (None, None, None). Mirrors
    the racing combined-pool approach: one model call sees all candidates and
    picks the globally closest, and the selection id maps back to its market.
    """
    union, owner = [], {}
    for m in markets:
        for r in m.get("runners", []):
            sid = r["selectionId"]
            if sid not in owner:
                owner[sid] = m
                union.append(r)
    picked = AIInterpreter.select_market_runner(user_input, union)
    if picked is None:
        return None, None, None
    m = owner.get(picked)
    if m is None:
        return None, None, None
    runner = next((r for r in m["runners"] if r["selectionId"] == picked), None)
    if runner is None:
        return None, None, None
    return m, picked, runner.get("runnerName", "")


def _repick_active(market: dict, active_ids: set, parsed_bet, user_input: str) -> tuple:
    """Re-resolve the runner among a market's ACTIVE runners only.

    Called when the catalogue match landed on a runner the live book shows as
    inactive (a withdrawn horse, or a re-listed market's stale duplicate whose
    name out-matched the live one). Returns (selection_id, runner_name) or
    (None, None) when no active runner matches.
    """
    runners = [r for r in market.get("runners", []) if r["selectionId"] in active_ids]
    sid, name = resolve_selection(runners, parsed_bet.selection_name)
    if sid is None:
        picked = AIInterpreter.select_market_runner(user_input or parsed_bet.selection_name, runners)
        if picked is not None and picked in active_ids:
            runner = next((r for r in runners if r["selectionId"] == picked), None)
            if runner is not None:
                return picked, runner.get("runnerName", "")
        return None, None
    return sid, name


def resolve_market(event_id: str, parsed_bet, session: dict, market_type: str, user_input: str = "") -> dict:
    markets = list_market_catalogue(event_id, market_type, session)

    print(f"DEBUG resolve_market - event={event_id} type={market_type} ({len(markets)} markets found)")
    print()

    if not markets:
        raise ValueError(f"No {market_type} market found for event {event_id}")

    if market_type.endswith("_LINE"):
        # Line markets don't name-match: take the first market's first runner and
        # let get_best_price pick the row by handicap.
        market = markets[0]
        first = market["runners"][0]
        selection_id, runner_name = first["selectionId"], first.get("runnerName", "")
    else:
        # Match across every market of the type (Limitation: alternate-line
        # markets used to be unreachable because only markets[0] was inspected).
        market, selection_id, runner_name = _match_by_name(markets, parsed_bet)
        if selection_id is None:
            # Substring matching fails when runner names don't contain the parsed
            # selection verbatim — common for markets whose runners are named per
            # competition (WINNING_MARGIN: "Hurricanes 15 +" vs "Northampton to
            # win by 15+"; HALF_TIME_FULL_TIME: "Hurricanes - Chiefs"). Hand the
            # runners (across all markets) to the AI to map the request.
            market, selection_id, runner_name = _match_by_ai(
                markets, user_input or parsed_bet.selection_name
            )
        print(f"  DEBUG resolved selectionId={selection_id} runner='{runner_name}'  (looking for name='{parsed_bet.selection_name}' line={parsed_bet.line})")
        print()

    if selection_id is None:
        raise ValueError(
            f"Runner '{parsed_bet.selection_name}' not found for event {event_id} ({market_type})."
        )

    # Fetch the book once, here, for two reasons: (1) drop runners the catalogue
    # still lists but the live market has marked inactive — a withdrawn horse or
    # a re-listed market's stale duplicate — and re-pick among ACTIVE runners;
    # (2) hand the same book to get_best_price so the price isn't fetched twice.
    book = get_market_book(market["marketId"], session)
    if not market_type.endswith("_LINE"):
        active_ids = {r["selectionId"] for r in book.get("runners", []) if r.get("status") == "ACTIVE"}
        if selection_id not in active_ids:
            selection_id, runner_name = _repick_active(market, active_ids, parsed_bet, user_input)
            print(f"  DEBUG re-picked active selectionId={selection_id} runner='{runner_name}'")
            print()
            if selection_id is None:
                raise ValueError(
                    f"Runner '{parsed_bet.selection_name}' is not an active runner in market {market['marketId']}."
                )

    return {
        "eventId": event_id,
        "marketId": market["marketId"],
        "selectionId": selection_id,
        "runnerName": runner_name,
        "competition": market.get("competition", {}).get("name"),
        "book": book,
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
