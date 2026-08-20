import re

from backend.config.sport_mapping import event_type_id_for
from backend.services.betfair_client import (
    get_market_winners,
    list_place_markets_for_event,
    list_racing_markets,
)
from backend.services.concurrency import parallel_map
from backend.services.market_resolver import resolve_selection

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
    "WINNER": "WIN",           # the football/outright code, occasionally emitted for racing
    "PLACE": "PLACE",
    "TO_BE_PLACED": "PLACE",
    "ANTEPOST_WIN": "ANTEPOST_WIN",
    "EACH_WAY": "EACH_WAY",    # Betfair's native each-way market — a single back settles win + place
}

# "top 3" style finishes. The parser is supposed to emit PLACE + places=N for
# these, but it also emits golf's TOP_<n>_FINISH family for racing — two real
# logged examples, "Shallow top 3 in york 1 pound" and "1 euro on Harry mole not
# to be top 3 Horse racing", both parsed as TOP_3_FINISH. Read the count out of
# the code rather than losing it.
_TOP_N_RE = re.compile(r"^TOP_(\d+)(?:_FINISH)?$")
# Same family, no count in the code — treat as a plain place bet.
_TOP_N_GENERIC = {"TOP_N_FINISH", "TOP_FINISH"}

UNSUPPORTED_RACING_DEFAULT = (
    "I don't support {label} bets on racing yet — try a win, place, each-way or "
    "ante-post bet on a single runner."
)


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


def racing_market_for(label: str | None, requested_places: int | None = None) -> tuple[str, int | None]:
    """Map a parsed market_type label to (Betfair racing market type, places).

    Raises UnsupportedRacingMarketError for anything not understood. That matters:
    this used to be `RACING_MARKET_MAP.get(label, "WIN")`, so an unrecognised
    label silently became a WIN bet — a "top 3" request would have gone on as
    money on the horse to win outright, a different market at different odds.
    Declining is the safe failure; placing a bet other than the one asked for is
    not.

    Kept as a standalone function so the offline audit (backend/eval) can ask the
    real mapping what it does instead of reimplementing it.
    """
    label = (label or "WIN").upper().strip()

    if label in RACING_UNSUPPORTED_MESSAGES:
        raise UnsupportedRacingMarketError(RACING_UNSUPPORTED_MESSAGES[label])

    if label in RACING_MARKET_MAP:
        return RACING_MARKET_MAP[label], requested_places

    top_n = _TOP_N_RE.match(label)
    if top_n:
        # An explicit places= from the parser wins; otherwise take the count
        # encoded in the market type ("TOP_3_FINISH" -> 3 places).
        return "PLACE", requested_places or int(top_n.group(1))

    if label in _TOP_N_GENERIC:
        return "PLACE", requested_places

    raise UnsupportedRacingMarketError(UNSUPPORTED_RACING_DEFAULT.format(label=label))


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
        "eventName": " — ".join(p for p in (meeting, race) if p),
        "marketStartTime": market.get("marketStartTime"),
        "marketType": market_type,
        "places": places,                            # number of places paid (place bets only)
    }


def _places_paid(market: dict, winners: dict) -> int | None:
    """Number of places a place market pays.

    Prefer numberOfWinners from the market book; when the book omits it (a market
    not yet open can return it as None), fall back to the count embedded in an
    OTHER_PLACE market name ('4 TBP' → 4). The standard 'To Be Placed' market
    carries no count in its name, so it stays None if the book didn't supply one.
    """
    n = winners.get(market["marketId"])
    if n is not None:
        return n
    m = re.search(r"(\d+)\s*TBP", market.get("marketName", ""), re.IGNORECASE)
    return int(m.group(1)) if m else None


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
        matching = [c for c in candidates if _places_paid(c[0], winners) == places]
        if not matching:
            return None
        chosen = next((c for c in matching if _is_standard(c[0])), matching[0])

    market, selection_id, runner_name = chosen
    return _racing_match(market, "PLACE", selection_id, runner_name, places=_places_paid(market, winners))


def _scope_markets(markets: list[dict], scope: str | None, *, fields: tuple[str, ...]) -> list[dict]:
    """Markets whose selected field(s) contain `scope` (case-insensitive substring).

    `fields` chooses what to match against: "event" (the meeting event name)
    and/or "market" (the market name). Returns [] when `scope` is blank or
    nothing matches — callers decide whether to fall back to the unscoped list,
    so a mis-typed track/festival name lets the runner name disambiguate rather
    than dead-ending.
    """
    scope = (scope or "").lower().strip()
    if not scope:
        return []
    out = []
    for m in markets:
        event_hit = "event" in fields and scope in m.get("event", {}).get("name", "").lower()
        market_hit = "market" in fields and scope in m.get("marketName", "").lower()
        if event_hit or market_hit:
            out.append(m)
    return out


def _scan_for_runner(pool: list[dict], selection_name: str, market_type: str) -> list[dict]:
    """Runner matches in a pool, one slip per market, with exact names preferred.

    Over the full-day scan (~450 markets) a short or common name substring-matches
    many unrelated runners via resolve_selection's loose matching, burying a clean
    exact hit and forcing a needless clarification. So take exact (case-insensitive)
    name equality across the whole pool first; only fall back to substring matching
    when nothing matches exactly — which preserves partial-name matches such as
    "Spirit" → "Spirit Dancer".
    """
    target = selection_name.lower().strip()
    exact, fuzzy = [], []
    for m in pool:
        runners = m.get("runners", [])
        hit = next((r for r in runners if r.get("runnerName", "").lower().strip() == target), None)
        if hit is not None:
            exact.append(_racing_match(m, market_type, hit["selectionId"], hit.get("runnerName", "")))
            continue
        selection_id, runner_name = resolve_selection(runners, selection_name)
        if selection_id is not None:
            fuzzy.append(_racing_match(m, market_type, selection_id, runner_name or ""))
    return exact or fuzzy


def _antepost_pool(event_type_id, parsed_bet, session: dict) -> list[dict]:
    """The ante-post markets to search for a future-race horse, scoped to the
    named race/festival.

    Used when a plain win bet names a horse that has no WIN market yet because
    it's entered for a future race (Gold Cup, Derby, Royal Ascot…). Ante-post
    markets carry market type ANTEPOST_WIN and are named after the race/festival.
    The scope can land in either field — `competition` ("the Gold Cup") or
    `event_name` ("Royal Ascot", which looks like a venue to the parser) — so we
    match both the market name and the event name; if neither matches we fall
    back to all of them and let the runner name (and match count) disambiguate.
    """
    markets = list_racing_markets(event_type_id, "ANTEPOST_WIN", session)
    scope = parsed_bet.competition or parsed_bet.event_name
    return _scope_markets(markets, scope, fields=("market", "event")) or markets


def _antepost_exact(pool: list[dict], parsed_bet) -> list[dict]:
    """Exact-preferred runner-name matches in the ante-post pool — one slip per market."""
    return _scan_for_runner(pool, parsed_bet.selection_name, "ANTEPOST_WIN")


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
    from backend.services.ai_interpreter import (
        AIInterpreter,  # local import: ai_interpreter is heavy (OpenAI client)
    )

    event_type_id = event_type_id_for(parsed_bet.sport)

    # Raises UnsupportedRacingMarketError rather than silently falling back to a
    # WIN bet, and recovers the places count out of a TOP_<n>_FINISH label.
    market_type, places = racing_market_for(parsed_bet.market_type, parsed_bet.places)

    def _finalize(found: list[dict]) -> list[dict]:
        """Place bets: per matched race, swap the standard PLACE market for the
        one paying the requested number of places (and tag places paid). The
        main scan matches one PLACE market per race, so `found` is already one
        per race — this just refines which place market within each."""
        if market_type != "PLACE":
            return found
        # Each race costs two serial Betfair calls (list_place_markets_for_event
        # then get_market_winners), and the races are independent — overlap them.
        # parallel_map preserves input order, so `refined` stays in the same
        # FIRST_TO_START order the serial loop produced.
        refined = []
        for chosen, exc in parallel_map(
            lambda m: _select_place_market(
                m["eventId"], parsed_bet.selection_name, places, session
            ),
            found,
        ):
            if exc is not None:
                raise exc
            if chosen:
                refined.append(chosen)
        if not refined:
            raise RacingClarificationError(
                f"'{parsed_bet.selection_name}' doesn't have a market paying {places} places. "
                "Try a different number of places, or a plain place bet."
            )
        return refined

    markets = list_racing_markets(event_type_id, market_type, session)

    # Scope to the named meeting when one was given. Substring match on the
    # event name ("Ascot" matches "Ascot 12th Jun"); if nothing matches the
    # supposed track name, fall back to the full scan rather than dead-ending.
    meeting_markets = _scope_markets(markets, parsed_bet.event_name, fields=("event",))
    scoped = meeting_markets or markets

    matches = _scan_for_runner(scoped, parsed_bet.selection_name, market_type)

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
