from backend.services.betfair_client import list_racing_markets, list_place_markets_for_event, get_market_winners
from backend.services.market_resolver import resolve_selection
from backend.config.sport_mapping import SPORT_EVENT_TYPE_MAP

# ── Racing (horse / greyhound) ────────────────────────────────────────────────
# Racing bets resolve by runner name, not event name, so they skip the AI
# event pick. Parser labels map to the single-runner Betfair market types we
# support; everything else gets a clean "not supported" rather than a forced
# (and possibly wrong) WIN match.

# Max slips to return when the runner name matches several races (partial
# names, greyhound name collisions). Mirrors the existing top-3 event cap —
# each slip costs a market-book call, and more than 3 isn't a usable choice.
MAX_RACING_MATCHES = 3

RACING_MARKET_MAP = {
    "WIN": "WIN",
    "MATCH_ODDS": "WIN",       # parser's generic default — for racing it means a straight win bet
    "OUTRIGHT_WINNER": "WIN",
    "PLACE": "PLACE",
    "TO_BE_PLACED": "PLACE",
    "ANTEPOST_WIN": "ANTEPOST_WIN",
    "EACH_WAY": "EACH_WAY",    # Betfair's native each-way market — a single back settles win + place
}

RACING_UNSUPPORTED_MESSAGES = {
    "FORECAST": "Forecast bets aren't supported yet — try a win or place bet on a single runner.",
    "REV_FORECAST": "Reverse forecast bets aren't supported yet — try a win or place bet on a single runner.",
    "TRICAST": "Tricast bets aren't supported yet — try a win or place bet on a single runner.",
    "MATCH_BET": "Head-to-head match bets aren't supported yet — try a win or place bet.",
    "RACE_WIN_DIST": "Winning distance bets aren't supported yet.",
    "WITHOUT_FAV": "Betting without the favourite isn't supported yet.",
    "SPECIAL": "Special racing markets aren't supported yet.",
}


class UnsupportedRacingMarketError(Exception):
    """Bet parsed to a racing market type the app deliberately doesn't place."""


class RacingClarificationError(Exception):
    """Runner couldn't be resolved unambiguously — ask the user for the meeting."""

    def __init__(self, question: str):
        self.question = question
        super().__init__(question)


def _racing_match(market: dict, market_type: str, selection_id: int, runner_name: str, places: int | None = None) -> dict:
    event = market.get("event", {})
    meeting = event.get("name", "")
    race = market.get("marketName", "")
    return {
        "eventId": event.get("id"),
        "marketId": market["marketId"],
        "selectionId": selection_id,
        "runnerName": runner_name,
        "competition": meeting,                      # meeting, e.g. "Ascot 12th Jun"
        "eventName": f"{meeting} — {race}".strip(" —"),
        "marketStartTime": market.get("marketStartTime"),
        "marketType": market_type,
        "places": places,                            # number of places paid (place bets only)
    }


def _select_place_market(event_id, selection_name: str, places: int | None, session: dict) -> dict | None:
    """Pick the right place market for a horse at one race and tag its places paid.

    A race has one standard PLACE market ('To Be Placed') plus OTHER_PLACE
    alternates ('2 TBP', '4 TBP'), distinguished by numberOfWinners (off the
    book). With `places` set, return the market paying exactly that many; with
    `places` None, return the standard PLACE market. Returns None if the race
    doesn't offer the requested number of places.
    """
    markets = list_place_markets_for_event(event_id, session)
    # Keep only this horse's markets (a meeting has many races; the horse runs once).
    candidates = []
    for m in markets:
        selection_id, runner_name = resolve_selection(m.get("runners", []), selection_name)
        if selection_id is not None:
            candidates.append((m, selection_id, runner_name or ""))
    if not candidates:
        return None

    winners = get_market_winners([m["marketId"] for m, _, _ in candidates], session)

    def _is_standard(m):
        return m.get("description", {}).get("marketType") == "PLACE"

    if places is None:
        chosen = next((c for c in candidates if _is_standard(c[0])), candidates[0])
    else:
        # Prefer the standard market on a tie; else any market paying `places`.
        matching = [c for c in candidates if winners.get(c[0]["marketId"]) == places]
        if not matching:
            return None
        chosen = next((c for c in matching if _is_standard(c[0])), matching[0])

    market, selection_id, runner_name = chosen
    return _racing_match(market, "PLACE", selection_id, runner_name, places=winners.get(market["marketId"]))


def _antepost_pool(event_type_id, parsed_bet, session: dict) -> list[dict]:
    """The ante-post markets to search for a future-race horse, scoped to the
    named race/festival.

    Used when a plain win bet names a horse that has no WIN market yet because
    it's entered for a future race (Gold Cup, Derby, Royal Ascot…). Ante-post
    markets carry market type ANTEPOST_WIN and are named after the race/festival.
    The scope can land in either field — `competition` ("the Gold Cup") or
    `event_name` ("Royal Ascot", which looks like a venue to the parser) — so we
    try both; if neither matches a market name we fall back to all of them and
    let the runner name (and match count) disambiguate.
    """
    markets = list_racing_markets(event_type_id, "ANTEPOST_WIN", session)
    scope = (parsed_bet.competition or parsed_bet.event_name or "").lower().strip()
    scoped = [
        m for m in markets
        if scope in m.get("marketName", "").lower()
        or scope in m.get("event", {}).get("name", "").lower()
    ] if scope else markets
    return scoped or markets


def _antepost_exact(pool: list[dict], parsed_bet) -> list[dict]:
    """Exact runner-name matches in the ante-post pool — one slip per market."""
    matches = []
    for m in pool:
        selection_id, runner_name = resolve_selection(m.get("runners", []), parsed_bet.selection_name)
        if selection_id is not None:
            matches.append(_racing_match(m, "ANTEPOST_WIN", selection_id, runner_name or ""))
    return matches


def resolve_racing_markets(parsed_bet, user_input: str, session: dict) -> list[dict]:
    """Find the race(s) containing the named horse/dog. No AI in the common case.

    Tier 1: scan every upcoming market's runners for the name. A horse runs
    at most once per day, so a clean match identifies the race by itself.
    0 matches or too many → RacingClarificationError asking for the meeting.

    Tier 2: when the bet names a meeting (clarification retries arrive with
    event_name = the track), scope to that meeting's races; if exact matching
    still finds nothing there, hand the meeting's ~8 race cards to the AI for
    typo-tolerant runner matching.

    WIN / PLACE / ANTEPOST_WIN / EACH_WAY are all single-runner markets resolved
    identically — only the market type fetched differs.
    """
    from backend.services.ai_interpreter import AIInterpreter  # local import: ai_interpreter is heavy (OpenAI client)

    sport = parsed_bet.sport.lower()
    event_type_id = SPORT_EVENT_TYPE_MAP.get(sport)
    if not event_type_id:
        raise ValueError(f"Unsupported sport: {sport}")

    label = (parsed_bet.market_type or "WIN").upper()
    if label in RACING_UNSUPPORTED_MESSAGES:
        raise UnsupportedRacingMarketError(RACING_UNSUPPORTED_MESSAGES[label])
    market_type = RACING_MARKET_MAP.get(label, "WIN")

    def _finalize(found: list[dict]) -> list[dict]:
        """Place bets: per matched race, swap the standard PLACE market for the
        one paying the requested number of places (and tag places paid). The
        main scan matches one PLACE market per race, so `found` is already one
        per race — this just refines which place market within each."""
        if market_type != "PLACE":
            return found
        refined = []
        for m in found:
            chosen = _select_place_market(m["eventId"], parsed_bet.selection_name, parsed_bet.places, session)
            if chosen:
                refined.append(chosen)
        if not refined:
            raise RacingClarificationError(
                f"'{parsed_bet.selection_name}' doesn't have a market paying {parsed_bet.places} places. "
                "Try a different number of places, or a plain place bet."
            )
        return refined

    markets = list_racing_markets(event_type_id, market_type, session)

    # Scope to the named meeting when one was given. Substring match on the
    # event name ("Ascot" matches "Ascot 12th Jun"); if nothing matches the
    # supposed track name, fall back to the full scan rather than dead-ending.
    meeting = (parsed_bet.event_name or "").lower().strip()
    meeting_markets = []
    if meeting:
        meeting_markets = [
            m for m in markets if meeting in m.get("event", {}).get("name", "").lower()
        ]
    scoped = meeting_markets or markets

    matches = []
    for m in scoped:
        selection_id, runner_name = resolve_selection(m.get("runners", []), parsed_bet.selection_name)
        if selection_id is not None:
            matches.append(_racing_match(m, market_type, selection_id, runner_name or ""))

    if matches:
        if len(matches) <= MAX_RACING_MATCHES:
            return _finalize(matches)
        if meeting_markets:
            # Already scoped to one meeting; can't narrow further — show the
            # first races (FIRST_TO_START order) and let the user pick.
            return _finalize(matches[:MAX_RACING_MATCHES])
        raise RacingClarificationError(
            f"I found {len(matches)} runners matching '{parsed_bet.selection_name}'. "
            "Which meeting is it running at?"
        )

    # 0 exact WIN matches. A win bet on a horse entered only for a future race
    # resolves via ante-post. The festival may ALSO have WIN markets for its
    # *other* races (Royal Ascot's WIN cards appear days ahead), so the meeting
    # filter above can be non-empty while the target horse lives only in
    # ante-post — build the ante-post pool up front. The scope can be in
    # `competition` ("the Gold Cup") or `event_name` ("Royal Ascot", read as a
    # venue by the parser); no scope → no ante-post, just ask for the meeting.
    scope = parsed_bet.competition or parsed_bet.event_name
    ap_pool = (
        _antepost_pool(event_type_id, parsed_bet, session)
        if market_type == "WIN" and scope else []
    )

    # An EXACT ante-post name match beats any fuzzy guess over the WIN card: a
    # horse named exactly as typed in a future race is the answer even when the
    # meeting also has WIN markets (the bug this guards against — Senorita Bonita
    # sits in Royal Ascot's ante-post Queen Mary Stakes while other Royal Ascot
    # races already have WIN cards, so a meeting-scoped AI pick would otherwise
    # hallucinate a different horse from the wrong race).
    ap_exact = _antepost_exact(ap_pool, parsed_bet) if ap_pool else []
    if ap_exact:
        if len(ap_exact) <= MAX_RACING_MATCHES:
            return ap_exact
        raise RacingClarificationError(
            f"I found {len(ap_exact)} ante-post markets with '{parsed_bet.selection_name}'. "
            "Which race is it for?"
        )

    # No exact hit anywhere (WIN or ante-post). Fall back to ONE typo-tolerant
    # AI pass over both pools at once — the meeting's WIN card and the festival's
    # ante-post markets — so a misspelled name resolves to whichever pool it's
    # actually in. A fixed order biases wrong (WIN-first hallucinates a WIN-card
    # horse when the target is in ante-post; ante-post-first does the reverse), so
    # the model sees every real candidate in one call and picks the closest.
    type_by_id, fuzzy_pool = {}, []
    for m, mtype in (
        [(m, market_type) for m in meeting_markets]
        + [(m, "ANTEPOST_WIN") for m in ap_pool]
    ):
        mid = m["marketId"]
        if mid in type_by_id:
            continue   # dedup; the meeting (WIN) entry is added first and wins
        type_by_id[mid] = mtype
        fuzzy_pool.append(m)

    if fuzzy_pool:
        pick = AIInterpreter.select_racing_runner(user_input, fuzzy_pool)
        if pick:
            # Trust the AI's selection_id (to recover the runner NAME), not its
            # market_id: when a horse is entered in several races the model
            # cross-wires the pair (market from race A, selection from race B),
            # which used to leave the runner unresolvable → spurious 422.
            chosen_name = next(
                (r.get("runnerName", "") for m in fuzzy_pool for r in m.get("runners", [])
                 if r.get("selectionId") == pick["selection_id"]),
                None,
            )
            if chosen_name:
                # Deterministic exact re-scan by name keeps market+selection
                # consistent and returns one slip per race the horse is entered
                # in (ante-post 2yos often hold 2-3 engagements), same as the
                # exact path's multi-match handling.
                found = [
                    _racing_match(m, type_by_id[m["marketId"]], sid, rn or "")
                    for m in fuzzy_pool
                    for sid, rn in [resolve_selection(m.get("runners", []), chosen_name)]
                    if sid is not None
                ]
                if found:
                    if len(found) <= MAX_RACING_MATCHES:
                        return _finalize(found)
                    raise RacingClarificationError(
                        f"I found {len(found)} races with '{chosen_name}'. Which race is it for?"
                    )

    # Nothing matched — tailor the clarification to what we actually searched.
    if ap_pool:
        raise RacingClarificationError(
            f"I couldn't find '{parsed_bet.selection_name}' in upcoming races or in "
            f"the {scope} ante-post markets. Could you check the name or the race?"
        )
    if meeting_markets:
        raise RacingClarificationError(
            f"I couldn't find '{parsed_bet.selection_name}' at {parsed_bet.event_name}. "
            "Could you check the runner's name?"
        )
    raise RacingClarificationError(
        f"I couldn't find '{parsed_bet.selection_name}' in any upcoming race. "
        "Which meeting is it running at?"
    )
