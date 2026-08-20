"""Natural-language market search agent + intent classifier.

The agent runs a provider-agnostic tool-calling loop (via llm.py) over the four
deterministic search tools. It navigates, prices a focused slice within budget,
then calls `present_results` to return a reply plus the markets to show. It has
NO placement tool — placement stays the user-driven /api/prepare-from-market →
confirm path. Cards are reconstructed server-side from the agent's own
price_markets output, so the agent can only surface real, priced selections.
"""
import json
import logging
import re
import time
from datetime import UTC, datetime

from backend.services.llm import get_llm
from backend.services.search_tools import SearchTools

log = logging.getLogger(__name__)

# gpt-4o-mini: handles this tool-calling loop well, is cheaper, and has far more
# token-per-minute headroom than gpt-4o (whose low-tier TPM a broad query — e.g. a
# day's racing — can exhaust). Bump back to "gpt-4o" if you raise your OpenAI tier
# and want stronger reasoning on complex queries.
AGENT_MODEL = "gpt-4o-mini"
CLASSIFIER_MODEL = "gpt-4o-mini"

MAX_TOOL_ROUNDS = 5      # caps loop length (latency/cost)
MAX_PRICE_CALLS = 2      # caps expensive price_markets calls per query (data budget)
# Cap on how many markets list_markets feeds back to the model PER CALL. Markets
# arrive liquidity-ranked (MAXIMUM_TRADED), so this keeps the most-traded ones and
# trims the long tail. Without it a broad query (e.g. a World Cup match has 60+
# markets) can dump hundreds of rows into the context, stalling — or rate-limiting
# — the next LLM round. Full data stays cached server-side for pricing/salvage.
MAX_MARKETS_TO_MODEL = 25
# When the model ends without surfacing cards but has listed this few or fewer
# markets, price them ourselves on exit rather than returning a prose answer.
AUTO_PRICE_LIMIT = 25

# The events/markets render as cards below the reply, so the reply box only needs
# a short intro. We trim it so it can never become a wall of text, no matter what
# the model returns.
MAX_REPLY_LINES = 5
# Lines that belong in the cards, not the reply: markdown bullets/headings and
# numbered/"R1"-style race rows. Dropping these first means the cap trims the
# duplicated list rather than chopping the real sentences off mid-answer.
_LIST_LINE_RE = re.compile(r"^\s*(?:[•\-\*]|#{1,6}\s|\d+[.)]\s|R\d+\b)", re.IGNORECASE)

# Fallback reply text for the salvage paths, used ONLY when the model left no
# usable reply of its own. Positive when salvage recovered something to show;
# apologetic only when there is genuinely nothing — so the apology can never sit
# above a list of correct cards.
_SALVAGE_OK_REPLY = "Here are the markets I found:"
_SALVAGE_EVENTS_REPLY = "Here are the events I found:"
_SALVAGE_EMPTY_REPLY = "I couldn't finish that search — try narrowing it."


TOOL_DEFS = [
    {
        "name": "find_events",
        "description": (
            "Find betting events (matches, races, tournaments) for a sport, "
            "optionally narrowed by a competition/team text and a start-time "
            "window. Call this first to locate the events the query is about."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "e.g. 'Football', 'Tennis', 'Horse Racing'."},
                "text": {"type": "string", "description": "Optional NAME text matched against event names (e.g. 'England' matches 'England v Ghana'). Use the MOST SPECIFIC name: prefer a named team/club/nation over a competition when both appear ('England world cup games' → 'England', not 'World Cup'). Use a competition only when no team is named. NEVER put a country/location here."},
                "country": {"type": "string", "description": "Optional ISO 3166-1 alpha-2 code that filters by WHERE THE EVENT IS HELD (host venue), e.g. 'AU', 'GB', 'IE', 'US'. Use ONLY for an explicit 'in <place>' query (mostly racing: 'greyhound racing in Australia'). NEVER set it from a team/nation name — a national team is a participant, not a location, and may play abroad (put it in `text` instead)."},
                "time_from": {"type": "string", "description": "Optional ISO 8601 UTC lower bound for start time, e.g. '2026-06-17T17:00:00Z'."},
                "time_to": {"type": "string", "description": "Optional ISO 8601 UTC upper bound for start time."},
            },
            "required": ["sport"],
        },
    },
    {
        "name": "find_outrights",
        "description": (
            "Find COMPETITION-LEVEL / outright markets in which a named team, club, "
            "nation, or individual is a competitor — markets where they are a RUNNER "
            "rather than named in the event. Covers not just 'X to win the "
            "tournament' (WINNER/OUTRIGHT_WINNER) but the whole competition: to "
            "finish top 5/10/20, make the cut, each-way, win their group, "
            "qualify / reach the final, top nationality, golden boot / top "
            "goalscorer, tournament match bets, etc. Use this WHENEVER the query "
            "centres on a participant's name, IN ADDITION to find_events — which "
            "only matches the participant in fixture NAMES ('England v France') and "
            "CANNOT see these. Each returned row carries its market_type (e.g. "
            "WINNER, TOP_10_FINISH, TO_REACH_FINAL) and the participant's "
            "selection_id; PRESENT the rows that fit the query — for a bare name "
            "show the winner(s) plus other notable markets, for a specific ask "
            "('top 10 finish', 'to reach the final') show that market type. Pass "
            "their market_ids (with the given selection_id) straight to "
            "present_results, which prices them. Cheap, no prices."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "e.g. 'Football', 'Golf', 'Tennis', 'Cricket'."},
                "name": {"type": "string", "description": "The team/club/nation/individual to find as a competitor, e.g. 'England', 'Scottie Scheffler', 'Arsenal'. Use the participant's name exactly — NOT a competition name."},
            },
            "required": ["sport", "name"],
        },
    },
    {
        "name": "list_market_types",
        "description": (
            "List the market type codes available across the given events "
            "(e.g. MATCH_ODDS, OVER_UNDER_25, BOTH_TEAMS_TO_SCORE). Cheap, no "
            "prices. Use to decide which market types answer the query."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["event_ids"],
        },
    },
    {
        "name": "list_markets",
        "description": (
            "List the actual markets (with runners, ranked by money matched) for "
            "the given events, optionally filtered to specific market type codes. "
            "Cheap, no prices. Use before pricing to pick which markets to price."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_ids": {"type": "array", "items": {"type": "string"}},
                "market_types": {"type": "array", "items": {"type": "string"}, "description": "Optional market type codes to filter to."},
            },
            "required": ["event_ids"],
        },
    },
    {
        "name": "price_markets",
        "description": (
            "Fetch live prices (best back/lay + size) BEFORE presenting. Needed "
            "ONLY when the query filters or ranks by PRICE (e.g. 'odds better than "
            "2.0', 'shortest favourites', 'best value') and you must see prices to "
            "decide which markets/runners to show. For normal display you do NOT "
            "need this — present_results prices whatever markets you include "
            "automatically. The only expensive call; budget-capped to the most-"
            "traded markets; if the result is truncated, tell the user to narrow "
            "the query. Pass market_ids you obtained from list_markets."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "market_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["market_ids"],
        },
    },
]

PRESENT_TOOL = {
    "name": "present_results",
    "description": (
        "Present the final answer. Provide a short reply plus EITHER markets (for "
        "small, specific sets) OR a navigable list of events (for broad 'what's "
        "on' sets — the user clicks an event to see its markets). The markets you "
        "include are priced AUTOMATICALLY here, so you do NOT need to price them "
        "first. Only include market_ids you got from list_markets, and event_ids "
        "you got from find_events."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "Short natural-language answer. Note if results were truncated and suggest narrowing."},
            "markets": {
                "type": "array",
                "description": "Markets to show as bettable rows (priced automatically on submit). Use for small, specific sets, or when the user wants odds.",
                "items": {
                    "type": "object",
                    "properties": {
                        "market_id": {"type": "string"},
                        "selection_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Optional specific runners to show; omit to show all runners in the market.",
                        },
                    },
                    "required": ["market_id"],
                },
            },
            "events": {
                "type": "array",
                "description": (
                    "Events/meetings to show as a navigable list (NO prices). Use this "
                    "instead of markets for broad sets (many events/races/competitions) "
                    "so nothing is priced until the user picks one to drill into."
                ),
                "items": {
                    "type": "object",
                    "properties": {"event_id": {"type": "string"}},
                    "required": ["event_id"],
                },
            },
        },
        "required": ["reply"],
    },
}


CLASSIFY_SYSTEM = (
    "Classify the user's message as either 'bet' or 'search'.\n"
    "'bet' = a specific instruction to place a bet — names a selection and "
    "usually a stake (e.g. 'back Arsenal £20', 'lay Liverpool a tenner', "
    "'Chelsea to win 50 at 2.4').\n"
    "'search' = a request to browse, explore, compare, or ask about markets, "
    "odds, fixtures, or whether/when events are on — without committing to one "
    "specific bet (e.g. 'show me the world cup markets', 'what are the over/under "
    "lines tonight', 'is there an England game tonight', 'shortest favourites in "
    "tennis today').\n"
    "Respond with exactly one word: bet or search."
)


def _agent_system_prompt() -> str:
    now = datetime.now(UTC)
    return f"""You are a Betfair Exchange market search assistant. The user asks, in free \
text, to browse or explore markets; you find the relevant markets and present them \
priced and ready to bet on. You do NOT place bets — the user does that from the results.

The current UTC time is {now.strftime('%Y-%m-%dT%H:%M:%SZ')} ({now.strftime('%A %d %B %Y')}). \
ONLY set a time_from/time_to window when the query explicitly names a time or date \
("today", "tonight", "this evening", "tomorrow", "Saturday", "this weekend", a specific \
date, etc.). If the query names NO time, OMIT time_from and time_to entirely so ALL \
upcoming events are searched — do NOT default to today, or you will hide events on later \
days. When a time IS named, convert it to an ISO 8601 UTC window yourself: "this evening" \
≈ 17:00–23:00 local; treat local as UTC unless the query says otherwise.

PARTICIPANTS (very important). When the query centres on a TEAM, CLUB, NATION, or \
INDIVIDUAL — "England", "Scottie Scheffler", "Arsenal", "show me the odds for Argentina to \
win the world cup" — the user usually wants BOTH that participant's upcoming fixtures AND the \
competition-level markets where they are a competitor. Those competition markets list the \
participant as a RUNNER and are named after the COMPETITION, never the participant, so \
find_events/textQuery CANNOT see them. They are not just "to win the tournament" (WINNER) — \
find_outrights also returns the participant in placings (top 5/10/20, make the cut, each-way), \
group-stage and qualification/progression markets (win the group, to qualify, to reach the \
final), top-nationality/region, golden boot / top goalscorer, and tournament match bets. So \
for any participant query you MUST, in the SAME step, call BOTH: find_events(text=<participant>) \
for their fixtures, AND find_outrights(sport, name=<participant>) for the competition markets. \
Then present the fixtures (as events) together with the find_outrights rows (as markets, each \
filtered to its given selection_id so the card shows just that participant). Each find_outrights \
row carries its market_type, so present the ones that FIT the query: for a bare name show the \
winner(s) plus other notable markets; for a specific ask ("top 10 finish", "to reach the \
final", "to qualify") present that market type. Do NOT try to guess and search a competition \
NAME to find a participant — find_outrights does that for you by scanning runners. For an \
individual in a tournament sport (golf, tennis, darts, snooker) there is usually NO fixture \
named after them, so find_outrights is the main or only useful call. Reserve a bare \
find_events (no find_outrights) for queries that name no participant ("what football is on", \
"tonight's over/under lines").

Worked example — query "England": ROUND 1, call BOTH find_events(sport="Football", \
text="England") and find_outrights(sport="Football", name="England") together. ROUND 2, go \
STRAIGHT to present_results — do NOT write a prose summary and do NOT call list_markets first. \
Put the participant's fixtures in `events` (navigable — the user clicks to see that match's \
markets) and EVERY find_outrights row in `markets`, each with selection_ids set to that row's \
selection_id (so each card shows just England). So present_results gets: reply="England play \
France tonight — here's that match plus England's outright markets:"; events = the England v \
France event_id; markets = the World Cup winner market_id (selection_ids = England's selection \
id) and the Euros winner market_id (selection_ids = England's selection id). The same applies \
to a lone individual ("Scottie Scheffler"): present each find_outrights row as a \
selection-filtered market even when there are no fixtures. Finishing a participant query with \
a text message instead of present_results is a FAILURE — the results never reach the user.

The `country` arg of find_events filters by WHERE THE EVENT IS HELD (the host venue), \
NOT by a participant's nationality. It is an ISO 3166-1 alpha-2 code (Australia → "AU", \
UK → "GB", Ireland → "IE", USA → "US"). Use it ONLY when the user explicitly asks for \
events TAKING PLACE IN a place — almost always racing meetings: "greyhound racing in \
Australia" → find_events(sport="Greyhound Racing", country="AU") with NO text.

NEVER derive `country` from a team or nation's NAME. A national team (England, Brazil, \
Australia, India) is a PARTICIPANT, not a location — put it in `text` and leave `country` \
unset. It may be playing abroad, and a host-country filter would wrongly hide it (e.g. \
England at a World Cup hosted in the USA is NOT a "GB" event). So "England" → \
find_events(sport="Football", text="England"), country UNSET. Set `country` only for a \
clear "in <place>" location query, never just because a country word appears.

Choosing `text`: use the MOST SPECIFIC name in the query. A named team/club/nation \
narrows to just that side's fixtures (Betfair matches `text` against event names like \
"England v Ghana"). When BOTH a team and a competition appear, use the TEAM — it is far \
more selective. "England world cup games" → text="England" (returns England's fixtures), \
NOT text="World Cup" (which returns EVERY fixture in the tournament, then you can't tell \
which are England's). Use a competition as `text` only when no team is named (e.g. an \
outright "World Cup winner" query). If you ever do search broadly and get many fixtures, \
do NOT present them all — present_results with only the events that actually match the \
query (here, the ones with "England" in the name). If there is no obvious team/competition in the query, leave `text` unset and return all events for the sport.

Workflow:
1. find_events to locate the events (give a sport; for `text` use the MOST SPECIFIC name — prefer a named team over a competition; add country ONLY for an explicit "in <place>" host-location query, NEVER from a team/nation name; add a time window ONLY when the query names a time). When the query names a participant, ALSO call find_outrights(sport, name) in this SAME step — it returns the win-the-tournament markets find_events cannot see.
2. list_market_types and/or list_markets to see what fixture markets are offered. Filter by market type when the query names one. (find_outrights rows are already bettable markets — they do NOT need list_markets.)
3. present_results with a short reply and the markets to show. Include the find_outrights rows as `markets` (each with its `selection_ids` set to the returned selection_id, so the card shows just that participant). The markets you include are priced AUTOMATICALLY on submit — you do NOT need to fetch prices first, so do NOT call price_markets just to display odds. (price_markets is ONLY for queries that filter or rank by price — see its description.)

Market-type hints (Betfair codes): match result → MATCH_ODDS; both teams to score → \
BOTH_TEAMS_TO_SCORE; football goals over/under → OVER_UNDER_05/15/25/35/45 (one market per line); \
large totals (basketball etc.) → COMBINED_TOTAL; correct score → CORRECT_SCORE; outright/tournament \
winner → competition-level event, market WINNER or OUTRIGHT_WINNER.

Showing relevant markets is the default — always include them when they exist. The reply \
field is also a real conversational answer, so pair the markets with a natural sentence rather \
than leaving it blank. For a question like "is there an England game tonight?", check with \
find_events and then answer AND show: e.g. reply "Yes — England play France at 20:00, here are \
the main markets:" with that game's markets attached. Only return an EMPTY markets list when \
there genuinely is nothing to show (find_events is empty, or the question isn't about bettable \
markets) — then just reply in plain language, e.g. "No, England aren't playing tonight."

Avoid pricing huge sets. If a query spans many events/races/competitions (e.g. "what horse \
races are on tomorrow", "what football is on today"), DO NOT list and price everything — that \
wastes tokens and Betfair data. Instead call find_events and present the events as a navigable \
list (the `events` argument); the user clicks one to see and price its markets. Reserve `markets` \
(pricing) for small, specific sets — a few named games, or when the user explicitly asks for \
odds/lines. When unsure because the set is large, prefer presenting events.

Rules:
- Only present markets you got from list_markets or find_outrights, and events you got from find_events. Never invent prices, runners, markets, or fixtures.
- Put events/markets in the `events`/`markets` arguments — never enumerate them in the reply text. The reply is a one-line intro only (e.g. "Here are tomorrow's meetings:"), since the list renders below it.
- You MUST end by calling present_results. Do NOT write a normal text message that lists markets, runners, or prices — that output is discarded. The ONLY way results reach the user is via the present_results arguments.
- Answer truthfully from what the tools return: if find_events comes back empty, say there's nothing on — do not invent an event.
- Pricing is budget-capped and may truncate. If you call price_markets (only for price-filter queries) and it returns `truncated`, say so in your reply and suggest a narrower query.
- Keep the reply short (1–3 sentences). The markets themselves carry the detail.
- Always finish by calling present_results (with markets, or with an empty list plus a helpful reply)."""


class SearchAgent:
    @staticmethod
    def run(user_input: str, session: dict, history: list = None) -> dict:
        """Run the agent loop for one query. `history` is a compact list of prior
        {role, content} text turns (no tool calls) for conversational refinement.
        Returns {reply, cards}."""
        tools = SearchTools(session)
        priced_cache: dict = {}
        price_calls = 0

        llm = get_llm(model=AGENT_MODEL)
        all_tools = TOOL_DEFS + [PRESENT_TOOL]
        # Built once so the system block is byte-identical across every round of
        # this query (the embedded timestamp is fixed for the query's lifetime).
        # A stable prefix is what lets the provider cache it across the loop's
        # serial calls instead of reprocessing it each round.
        system = _agent_system_prompt()

        messages = list(history or [])
        messages.append({"role": "user", "content": user_input})

        # Metrics for the searches log: total wall time, the LLM-bound slice of it
        # (so latency can be attributed to model vs Betfair calls), and how hard
        # the loop worked. Stamped onto the result at every exit via _finish.
        run_start = time.monotonic()
        llm_ms = 0

        def _finish(result: dict, rounds: int, salvaged: bool, hit_round_cap: bool = False) -> dict:
            result["metrics"] = {
                "query": user_input,
                "rounds": rounds,
                "hit_round_cap": hit_round_cap,
                "price_calls": price_calls,
                "total_latency_ms": int((time.monotonic() - run_start) * 1000),
                "llm_latency_ms": llm_ms,
                "cards": len(result.get("cards") or []),
                "events": len(result.get("events") or []),
                "salvaged": salvaged,
            }
            return result

        log.info("SearchAgent.run: %r (history: %d prior turn(s))", user_input, len(messages) - 1)

        for round_no in range(1, MAX_TOOL_ROUNDS + 1):
            llm_t0 = time.monotonic()
            resp = llm.complete(system, messages, tools=all_tools)
            llm_ms += int((time.monotonic() - llm_t0) * 1000)

            if not resp.tool_calls:
                # Model answered in plain text without calling present_results.
                # Surface structured results anyway (salvage) so the user gets
                # bettable cards rather than a prose list.
                log.info("agent round %d: no tool call — model replied in text, running salvage", round_no)
                return _finish(
                    SearchAgent._result(resp.text or "", [], [], tools, priced_cache),
                    rounds=round_no, salvaged=True,
                )

            log.debug("agent round %d: model chose %s", round_no, [tc.name for tc in resp.tool_calls])

            messages.append({
                "role": "assistant",
                "content": resp.text,
                "tool_calls": resp.tool_calls,
            })

            present_args = None
            for tc in resp.tool_calls:
                a = tc.arguments
                # The content fed back to the model is kept COMPACT (no full runner
                # arrays) so a broad query — e.g. a day's racing — can't balloon the
                # context past the model's token-per-minute limit. Full data stays
                # server-side: list_markets caches it, and cards are rebuilt from the
                # price cache, so compacting the model's view loses nothing.
                if tc.name == "present_results":
                    present_args = a
                    log.debug(
                        "agent → present_results: %d market(s), %d event(s)",
                        len(a.get("markets") or []), len(a.get("events") or []),
                    )
                    content = {"status": "ok"}
                elif tc.name == "price_markets":
                    price_calls += 1
                    ids = a.get("market_ids", [])
                    if price_calls > MAX_PRICE_CALLS:
                        log.warning(
                            "agent → price_markets BLOCKED — budget used (%d price calls)",
                            MAX_PRICE_CALLS,
                        )
                        content = {"error": "pricing budget reached for this query — narrow it and try again"}
                    else:
                        log.debug("agent → price_markets: pricing %d market(s)", len(ids))
                        full = tools.price_markets(ids)
                        for m in full["markets"]:
                            priced_cache[m["market_id"]] = m
                        log.debug(
                            "  price_markets result: shown=%s total=%s truncated=%s",
                            full["shown"], full["total"], full["truncated"],
                        )
                        content = SearchAgent._compact_priced(full)
                elif tc.name == "list_markets":
                    eids, types = a.get("event_ids", []), a.get("market_types")
                    log.debug("agent → list_markets: %d event(s), types=%s", len(eids), types or "ALL")
                    # Caches full data in `tools`; only the lean view goes to the model.
                    full = tools.list_markets(eids, types)
                    # Already liquidity-ranked upstream — cap the model's view to the
                    # most-traded so a broad all-types result can't stall the next round.
                    capped = full[:MAX_MARKETS_TO_MODEL]
                    note = "" if len(full) <= MAX_MARKETS_TO_MODEL else f" (showing top {MAX_MARKETS_TO_MODEL} by liquidity)"
                    log.debug("  list_markets result: %d market(s)%s", len(full), note)
                    content = SearchAgent._compact_markets(capped)
                elif tc.name == "find_events":
                    log.debug(
                        "agent → find_events: sport=%r text=%r country=%r from=%s to=%s",
                        a.get("sport"), a.get("text"), a.get("country"),
                        a.get("time_from"), a.get("time_to"),
                    )
                    content = SearchAgent._dispatch(tc, tools)
                    log.debug("  find_events result: %d event(s)", len(content))
                    if log.isEnabledFor(logging.DEBUG):
                        for ev in content:
                            comp = ev.get("competition")
                            log.debug(
                                "    • %s  %s%s  @ %s",
                                ev.get("event_id"), ev.get("name"),
                                f" [{comp}]" if comp else "", ev.get("open_date"),
                            )
                elif tc.name == "find_outrights":
                    log.debug("agent → find_outrights: sport=%r name=%r", a.get("sport"), a.get("name"))
                    content = SearchAgent._dispatch(tc, tools)
                    log.debug("  find_outrights result: %d market(s)", len(content))
                    if log.isEnabledFor(logging.DEBUG):
                        for mk in content:
                            log.debug(
                                "    • %s  %s — %s @ %s",
                                mk.get("market_id"), mk.get("runner_name"),
                                mk.get("market_name"), mk.get("event_name"),
                            )
                elif tc.name == "list_market_types":
                    log.debug("agent → list_market_types: %d event(s)", len(a.get("event_ids", [])))
                    content = SearchAgent._dispatch(tc, tools)
                    log.debug("  list_market_types result: %s", content)
                else:
                    content = SearchAgent._dispatch(tc, tools)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(content, default=str),
                })

            if present_args is not None:
                # NORMAL PATH (pricing fold): the model presents the markets it wants
                # WITHOUT pricing them first — it's instructed that present_results
                # prices them here. Cards come only from our own priced data (the
                # integrity gate), so we price the presented-but-unpriced markets now,
                # within budget, turning them into real cards. Any market the model
                # DID price itself (only price-filter queries call price_markets) is
                # already in priced_cache and skipped, so nothing is priced twice.
                unpriced = [
                    m.get("market_id") for m in (present_args.get("markets") or [])
                    if m.get("market_id") and m.get("market_id") not in priced_cache
                ]
                if unpriced and price_calls < MAX_PRICE_CALLS:
                    price_calls += 1
                    log.debug("present_results named %d unpriced market(s) — pricing them now", len(unpriced))
                    full = tools.price_markets(unpriced)
                    for m in full["markets"]:
                        priced_cache[m["market_id"]] = m
                return _finish(
                    SearchAgent._result(
                        present_args.get("reply", ""),
                        SearchAgent._build_cards(present_args, priced_cache),
                        SearchAgent._build_event_cards(present_args, tools),
                        tools,
                        priced_cache,
                    ),
                    rounds=round_no, salvaged=False,
                )

        log.warning(
            "agent reached MAX_TOOL_ROUNDS (%d) without present_results, running salvage",
            MAX_TOOL_ROUNDS,
        )
        # Empty reply: let _result pick the wording, so it only apologises when
        # salvage genuinely recovered nothing (not when it recovered cards).
        return _finish(
            SearchAgent._result("", [], [], tools, priced_cache),
            rounds=MAX_TOOL_ROUNDS, salvaged=True, hit_round_cap=True,
        )

    @staticmethod
    def _result(reply: str, cards: list, events: list, tools: SearchTools, priced_cache: dict) -> dict:
        """Final result with a deterministic salvage, so a search never degrades to
        a prose answer when the model navigated but failed to present:
          1. something already surfaced -> use it;
          2. else if markets were priced -> show those;
          3. else if a modest set of markets was listed -> price them now and show;
          4. else if events were found -> offer them as a navigable list."""
        reply = SearchAgent._clamp_reply(reply)
        if cards or events:
            log.info("Search presenting %d card(s), %d event(s)", len(cards), len(events))
            default = _SALVAGE_OK_REPLY if cards else _SALVAGE_EVENTS_REPLY
            return {"reply": reply or default, "cards": cards, "events": events}

        if priced_cache:
            synth = {"markets": [{"market_id": mid} for mid in priced_cache]}
            built = SearchAgent._build_cards(synth, priced_cache)
            if built:
                log.info(
                    "salvage: nothing presented — building cards from %d already-priced market(s)",
                    len(priced_cache),
                )
                return {"reply": reply or _SALVAGE_OK_REPLY, "cards": built,
                        "events": SearchAgent._leftover_events(built, tools)}

        listed = list(tools._markets.keys())
        if listed and len(listed) <= AUTO_PRICE_LIMIT:
            log.info("salvage: nothing priced — auto-pricing %d listed market(s)", len(listed))
            full = tools.price_markets(listed)
            for m in full["markets"]:
                priced_cache[m["market_id"]] = m
            synth = {"markets": [{"market_id": mid} for mid in priced_cache]}
            built = SearchAgent._build_cards(synth, priced_cache)
            if built:
                log.info("salvage produced %d card(s)", len(built))
                return {"reply": reply or _SALVAGE_OK_REPLY, "cards": built,
                        "events": SearchAgent._leftover_events(built, tools)}

        # Nothing backable to show — fall back to the events found, as navigation.
        evs = list(tools._events.values())
        log.info("salvage: nothing backable — falling back to %d navigable event(s)", len(evs))
        if evs:
            return {"reply": reply or _SALVAGE_EVENTS_REPLY, "cards": [], "events": evs}
        # Genuinely nothing recovered — this is the only place the apology appears.
        return {"reply": reply or _SALVAGE_EMPTY_REPLY, "cards": [], "events": []}

    @staticmethod
    def _clamp_reply(reply: str) -> str:
        """Keep the reply box short without cutting the answer off mid-sentence.
        First drop the list/heading lines the model sometimes inlines (they already
        render as cards below), then cap what's left to MAX_REPLY_LINES. If the
        whole reply was a list, keep just its first line as the intro."""
        if not reply:
            return reply
        lines = [ln.rstrip() for ln in reply.splitlines() if ln.strip()]
        prose = [ln for ln in lines if not _LIST_LINE_RE.match(ln)]
        kept = prose or lines[:1]
        return "\n".join(kept[:MAX_REPLY_LINES]).strip()

    @staticmethod
    def _dispatch(tc, tools: SearchTools):
        args = tc.arguments
        if tc.name == "find_events":
            return tools.find_events(
                args.get("sport", ""), args.get("text"),
                args.get("time_from"), args.get("time_to"),
                args.get("country"),
            )
        if tc.name == "find_outrights":
            return tools.find_outrights(args.get("sport", ""), args.get("name", ""))
        if tc.name == "list_market_types":
            return tools.list_market_types(args.get("event_ids", []))
        return {"error": f"unknown tool {tc.name}"}

    @staticmethod
    def _leftover_events(cards: list, tools: SearchTools) -> list:
        """Found events not already represented by a salvaged card's event. Lets a
        salvage that recovered (e.g.) outright cards for a participant still surface
        that participant's fixtures as navigable events, instead of dropping them —
        so a bare "England" search shows both the World Cup card AND the England
        match even when the model skipped present_results."""
        shown = {c.get("event_id") for c in cards}
        return [e for e in tools._events.values() if e.get("event_id") not in shown]

    @staticmethod
    def _build_event_cards(present_args: dict, tools: SearchTools) -> list:
        """Navigable event cards for the broad-query path, reconstructed from the
        find_events cache. Any event_id the agent names that wasn't actually
        returned by find_events is dropped (integrity gate)."""
        out = []
        for e in present_args.get("events") or []:
            meta = tools._events.get(e.get("event_id"))
            if meta:
                out.append(meta)
        return out

    @staticmethod
    def _compact_markets(markets: list) -> list:
        """Lean view of list_markets for the model: no runner arrays, just what it
        needs to choose which markets to price (ids, type, event, liquidity)."""
        return [
            {
                "market_id": m["market_id"],
                "market_name": m["market_name"],
                "market_type": m["market_type"],
                "event_id": m["event_id"],
                "event_name": m["event_name"],
                "market_start_time": m["market_start_time"],
                "total_matched": m["total_matched"],
                "num_runners": len(m["runners"]),
            }
            for m in markets
        ]

    @staticmethod
    def _compact_priced(result: dict) -> dict:
        """Lean view of price_markets for the model: top few runners with back
        prices only. The frontend still renders every runner from the full price
        cache, so capping here only trims the model's context, not the output."""
        out = {
            "total": result["total"],
            "shown": result["shown"],
            "truncated": result["truncated"],
            "markets": [],
        }
        for m in result["markets"]:
            runners = m["runners"]
            out["markets"].append({
                "market_id": m["market_id"],
                "event_name": m["event_name"],
                "market_type": m["market_type"],
                "market_start_time": m["market_start_time"],
                "num_runners": len(runners),
                "runners": [
                    {"selection_id": r["selection_id"], "name": r["runner_name"], "back": r["back_price"]}
                    for r in runners[:8]
                ],
            })
        return out

    @staticmethod
    def _build_cards(present_args: dict, priced_cache: dict) -> list:
        """Reconstruct bettable cards from the agent's chosen markets using the
        server's own priced data — the integrity gate. Any market_id the agent
        names that wasn't actually priced is dropped, and prices come from our
        tool output, never from the model."""
        cards = []
        for entry in present_args.get("markets", []):
            m = priced_cache.get(entry.get("market_id"))
            if not m:
                continue
            wanted = set(entry.get("selection_ids") or [])
            for r in m["runners"]:
                if wanted and r["selection_id"] not in wanted:
                    continue
                if r["back_price"] is None:
                    continue  # nothing backable -> not a bet-ready row
                cards.append({
                    "event_id": m["event_id"],
                    "event_name": m["event_name"],
                    "competition": m["competition"],
                    "market_start_time": m["market_start_time"],
                    "market_id": m["market_id"],
                    "market_type": m["market_type"],
                    "market_name": m["market_name"],
                    "selection_id": r["selection_id"],
                    "runner_name": r["runner_name"],
                    "handicap": r["handicap"],
                    "side": "BACK",
                    "price": r["back_price"],
                    "size": r["back_size"],
                    "lay_price": r["lay_price"],
                    "lay_size": r["lay_size"],
                })
        return cards


def classify_intent(user_input: str) -> str:
    """Route a single NL message to 'bet' (place a specific bet) or 'search'
    (browse/explore markets). Defaults to 'bet' when the model is unclear, since
    a misrouted search just fails to parse and re-prompts, whereas the bet path
    never places without explicit confirmation anyway."""
    llm = get_llm(model=CLASSIFIER_MODEL)
    resp = llm.complete(CLASSIFY_SYSTEM, [{"role": "user", "content": user_input}])
    text = (resp.text or "").strip().lower()
    intent = "search" if "search" in text else "bet"
    log.debug("classify_intent(%r) -> %s", user_input, intent)
    return intent
