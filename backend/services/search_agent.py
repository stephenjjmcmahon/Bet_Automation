"""Natural-language market search agent + intent classifier.

The agent runs a provider-agnostic tool-calling loop (via llm.py) over the four
deterministic search tools. It navigates, prices a focused slice within budget,
then calls `present_results` to return a reply plus the markets to show. It has
NO placement tool — placement stays the user-driven /api/prepare-from-market →
confirm path. Cards are reconstructed server-side from the agent's own
price_markets output, so the agent can only surface real, priced selections.
"""
import json
import re
import time
from datetime import datetime, timezone

from backend.services.llm import get_llm
from backend.services.search_tools import SearchTools

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
            "Fetch live prices (best back/lay + size) for the given markets. This "
            "is the only expensive call and is budget-capped to the most-traded "
            "markets; if the result is truncated, tell the user to narrow the "
            "query. Pass market_ids you obtained from list_markets."
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
        "Present the final answer. Provide a short reply plus EITHER priced "
        "markets (for small, specific sets) OR a navigable list of events (for "
        "broad 'what's on' sets — the user clicks an event to see its markets). "
        "Only include market_ids you priced via price_markets, and event_ids you "
        "got from find_events."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "Short natural-language answer. Note if results were truncated and suggest narrowing."},
            "markets": {
                "type": "array",
                "description": "Priced markets to show as bettable rows. Use for small, specific sets, or when the user wants odds.",
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
    now = datetime.now(timezone.utc)
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
query (here, the ones with "England" in the name).

Workflow:
1. find_events to locate the events (give a sport; for `text` use the MOST SPECIFIC name — prefer a named team over a competition; add country ONLY for an explicit "in <place>" host-location query, NEVER from a team/nation name; add a time window ONLY when the query names a time).
2. list_market_types and/or list_markets to see what's offered. Filter by market type when the query names one.
3. price_markets on the focused set of markets that answer the query.
4. present_results with a short reply and the markets to show.

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
- Only present markets you actually priced, and events you got from find_events. Never invent prices, runners, markets, or fixtures.
- Put events/markets in the `events`/`markets` arguments — never enumerate them in the reply text. The reply is a one-line intro only (e.g. "Here are tomorrow's meetings:"), since the list renders below it.
- You MUST end by calling present_results. Do NOT write a normal text message that lists markets, runners, or prices — that output is discarded. The ONLY way results reach the user is via the present_results arguments.
- Answer truthfully from what the tools return: if find_events comes back empty, say there's nothing on — do not invent an event.
- price_markets is budget-capped and may truncate; if `truncated` is true, say so in your reply and suggest a narrower query.
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

        print(f"DEBUG SearchAgent.run: {user_input!r}  (history: {len(messages) - 1} prior turn(s))")
        print()

        for round_no in range(1, MAX_TOOL_ROUNDS + 1):
            llm_t0 = time.monotonic()
            resp = llm.complete(system, messages, tools=all_tools)
            llm_ms += int((time.monotonic() - llm_t0) * 1000)

            if not resp.tool_calls:
                # Model answered in plain text without calling present_results.
                # Surface structured results anyway (salvage) so the user gets
                # bettable cards rather than a prose list.
                print(f"DEBUG agent round {round_no}: no tool call — model replied in text, running salvage")
                print()
                return _finish(
                    SearchAgent._result(resp.text or "", [], [], tools, priced_cache),
                    rounds=round_no, salvaged=True,
                )

            print(f"DEBUG agent round {round_no}: model chose {[tc.name for tc in resp.tool_calls]}")
            print()

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
                    print(f"DEBUG agent → present_results: {len(a.get('markets') or [])} market(s), {len(a.get('events') or [])} event(s)")
                    print()
                    content = {"status": "ok"}
                elif tc.name == "price_markets":
                    price_calls += 1
                    ids = a.get("market_ids", [])
                    if price_calls > MAX_PRICE_CALLS:
                        print(f"DEBUG agent → price_markets BLOCKED — budget used ({MAX_PRICE_CALLS} price calls)")
                        print()
                        content = {"error": "pricing budget reached for this query — narrow it and try again"}
                    else:
                        print(f"DEBUG agent → price_markets: pricing {len(ids)} market(s)")
                        full = tools.price_markets(ids)
                        for m in full["markets"]:
                            priced_cache[m["market_id"]] = m
                        print(f"  DEBUG price_markets result: shown={full['shown']} total={full['total']} truncated={full['truncated']}")
                        print()
                        content = SearchAgent._compact_priced(full)
                elif tc.name == "list_markets":
                    eids, types = a.get("event_ids", []), a.get("market_types")
                    print(f"DEBUG agent → list_markets: {len(eids)} event(s), types={types or 'ALL'}")
                    # Caches full data in `tools`; only the lean view goes to the model.
                    full = tools.list_markets(eids, types)
                    # Already liquidity-ranked upstream — cap the model's view to the
                    # most-traded so a broad all-types result can't stall the next round.
                    capped = full[:MAX_MARKETS_TO_MODEL]
                    note = "" if len(full) <= MAX_MARKETS_TO_MODEL else f" (showing top {MAX_MARKETS_TO_MODEL} by liquidity)"
                    print(f"  DEBUG list_markets result: {len(full)} market(s){note}")
                    print()
                    content = SearchAgent._compact_markets(capped)
                elif tc.name == "find_events":
                    print(f"DEBUG agent → find_events: sport={a.get('sport')!r} text={a.get('text')!r} country={a.get('country')!r} from={a.get('time_from')} to={a.get('time_to')}")
                    content = SearchAgent._dispatch(tc, tools)
                    print(f"  DEBUG find_events result: {len(content)} event(s)")
                    for ev in content:
                        comp = ev.get("competition")
                        comp_str = f" [{comp}]" if comp else ""
                        print(f"    • {ev.get('event_id')}  {ev.get('name')}{comp_str}  @ {ev.get('open_date')}")
                    print()
                elif tc.name == "list_market_types":
                    print(f"DEBUG agent → list_market_types: {len(a.get('event_ids', []))} event(s)")
                    content = SearchAgent._dispatch(tc, tools)
                    print(f"  DEBUG list_market_types result: {content}")
                    print()
                else:
                    content = SearchAgent._dispatch(tc, tools)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(content, default=str),
                })

            if present_args is not None:
                # The model can call present_results with markets it never priced
                # (skipping price_markets). Cards come only from our own priced data
                # (integrity gate), so those would all be dropped and the result
                # would wrongly fall back to navigable events. Price the presented-
                # but-unpriced markets now (within budget) so they become real cards.
                unpriced = [
                    m.get("market_id") for m in (present_args.get("markets") or [])
                    if m.get("market_id") and m.get("market_id") not in priced_cache
                ]
                if unpriced and price_calls < MAX_PRICE_CALLS:
                    price_calls += 1
                    print(f"DEBUG present_results named {len(unpriced)} unpriced market(s) — pricing them now")
                    print()
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

        print(f"DEBUG agent: reached MAX_TOOL_ROUNDS ({MAX_TOOL_ROUNDS}) without present_results, running salvage")
        print()
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
            print(f"DEBUG result: presenting {len(cards)} card(s), {len(events)} event(s)")
            print()
            default = _SALVAGE_OK_REPLY if cards else _SALVAGE_EVENTS_REPLY
            return {"reply": reply or default, "cards": cards, "events": events}

        if priced_cache:
            synth = {"markets": [{"market_id": mid} for mid in priced_cache]}
            built = SearchAgent._build_cards(synth, priced_cache)
            if built:
                print(f"DEBUG result salvage: nothing presented — building cards from {len(priced_cache)} already-priced market(s)")
                print()
                return {"reply": reply or _SALVAGE_OK_REPLY, "cards": built, "events": []}

        listed = list(tools._markets.keys())
        if listed and len(listed) <= AUTO_PRICE_LIMIT:
            print(f"DEBUG result salvage: nothing priced — auto-pricing {len(listed)} listed market(s)")
            print()
            full = tools.price_markets(listed)
            for m in full["markets"]:
                priced_cache[m["market_id"]] = m
            synth = {"markets": [{"market_id": mid} for mid in priced_cache]}
            built = SearchAgent._build_cards(synth, priced_cache)
            if built:
                print(f"  DEBUG salvage produced {len(built)} card(s)")
                print()
                return {"reply": reply or _SALVAGE_OK_REPLY, "cards": built, "events": []}

        # Nothing backable to show — fall back to the events found, as navigation.
        evs = list(tools._events.values())
        print(f"DEBUG result salvage: nothing backable — falling back to {len(evs)} navigable event(s)")
        print()
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
        if tc.name == "list_market_types":
            return tools.list_market_types(args.get("event_ids", []))
        return {"error": f"unknown tool {tc.name}"}

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
    print(f"DEBUG classify_intent({user_input!r}) -> {intent}")
    print()
    return intent
