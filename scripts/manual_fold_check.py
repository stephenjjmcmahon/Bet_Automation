"""Developer script — manual validation harness for the search agent's pricing fold.

Runs the REAL search agent (real gpt-4o-mini + the new prompt) over representative
natural-language queries, with Betfair mocked by a small in-memory fixture so it
needs no login and is deterministic on the data side. For each query it prints the
per-round tool sequence and the result metrics, so you can SEE that:

  * common display queries run WITHOUT calling price_markets (the fold) — pricing
    happens on present;
  * broad "what's on" queries return a navigable events list;
  * price-filter queries ("better than 2.0") still call price_markets, because the
    model must see prices to decide what qualifies;
  * PARTICIPANT queries ("England", "Scottie Scheffler") call find_outrights and
    surface the "to win the tournament" market (where the participant is a RUNNER,
    invisible to find_events) as a card — alongside their fixtures.

Run from the repo root:  python -m scripts.manual_fold_check
Needs OPENAI_API_KEY in .env. Does NOT touch Betfair (all client calls are mocked).
"""
from unittest.mock import patch

from backend.services import search_agent, search_tools
from backend.services.llm import OpenAIClient
from backend.services.search_agent import AGENT_MODEL, SearchAgent

# ── tiny in-memory Betfair fixture ──────────────────────────────────────────────
_EVENTS = {
    "1001": ("Arsenal v Chelsea", "English Premier League"),
    "1002": ("Liverpool v Man City", "English Premier League"),
    "1003": ("England v France", "International Friendly"),
}

# back price per selection id (last two digits) — a real spread around 2.0 so the
# "better than 2.0" filter has something to actually filter. The single-digit keys
# price the outright runners (selection ids 5000x / 5100x → %100 = 1..4).
_PRICES = {10: 1.80, 11: 3.50, 12: 4.20, 20: 2.02, 21: 1.96, 30: 1.70, 31: 2.20,
           1: 8.0, 2: 5.0, 3: 6.5, 4: 9.0}


# Competition-level markets per Betfair event-type id, as (event_id, competition,
# market_id, market_name, market_type, runners, total_matched). The participant
# (England, Scottie Scheffler) appears here ONLY as a runner — never in an event
# name — so find_events can't reach them; find_outrights scans these runners. The
# set spans winners AND non-winner markets (to-reach-final, top-10-finish) to show
# the broad scan + the agent curating which kind to present.
_OUTRIGHTS = {
    "1": [   # football (event_type_id 1)
        ("8001", "FIFA World Cup", "1.5001", "Winner", "WINNER",
         [(50001, "England"), (50002, "Brazil"), (50003, "France"), (50004, "Argentina")], 2_000_000),
        ("8002", "UEFA Euro 2028", "1.5002", "Winner", "WINNER",
         [(50011, "England"), (50012, "Germany"), (50013, "Spain")], 800_000),
        ("8001", "FIFA World Cup", "1.5003", "To Reach the Final", "TO_REACH_FINAL",
         [(50021, "England"), (50022, "Brazil"), (50023, "France")], 300_000),
    ],
    "3": [   # golf (event_type_id 3)
        ("8101", "The Open", "1.5101", "Winner", "WINNER",
         [(51001, "Scottie Scheffler"), (51002, "Rory McIlroy"), (51003, "Jon Rahm")], 500_000),
        ("8101", "The Open", "1.5102", "Top 10 Finish", "TOP_10_FINISH",
         [(51011, "Scottie Scheffler"), (51012, "Rory McIlroy")], 150_000),
    ],
}


def _mk(eid, ename, comp, mid, mname, mtype, total, runners):
    return {
        "marketId": mid, "marketName": mname, "totalMatched": total,
        "description": {"marketType": mtype},
        "event": {"id": eid, "name": ename}, "competition": {"name": comp},
        "marketStartTime": "2026-06-24T19:45:00.000Z",
        "runners": [{"selectionId": s, "runnerName": r, "handicap": 0.0} for s, r in runners],
    }


def _markets_for(eid):
    name, comp = _EVENTS[eid]
    home, away = name.split(" v ")
    base = int(eid) * 100
    return [
        _mk(eid, name, comp, f"1.{base+1}", "Match Odds", "MATCH_ODDS", 500000,
            [(base + 10, home), (base + 11, "The Draw"), (base + 12, away)]),
        _mk(eid, name, comp, f"1.{base+2}", "Over/Under 2.5 Goals", "OVER_UNDER_25", 480000,
            [(base + 20, "Over 2.5 Goals"), (base + 21, "Under 2.5 Goals")]),
        _mk(eid, name, comp, f"1.{base+3}", "Both teams to Score?", "BOTH_TEAMS_TO_SCORE", 300000,
            [(base + 30, "Yes"), (base + 31, "No")]),
    ]


def _price_for(sid):
    return _PRICES.get(sid % 100, 2.50)


def _outright_markets_for(event_type_id):
    """Full catalogue dicts for the outright markets of one event-type id."""
    return [
        _mk(eid, comp, comp, mid, mname, mtype, total, runners)
        for (eid, comp, mid, mname, mtype, runners, total) in _OUTRIGHTS.get(event_type_id, [])
    ]


def fake_list_events_filtered(event_type_id, session, text_query=None,
                              time_from=None, time_to=None, countries=None):
    # Only football (id 1) has H2H fixtures in this fixture; golf etc. have none —
    # so a golf player query relies entirely on find_outrights (as in reality).
    if event_type_id != "1":
        return []
    out = []
    for eid, (name, comp) in _EVENTS.items():
        if text_query and text_query.lower() not in name.lower():
            continue
        out.append({"event": {"id": eid, "name": name, "openDate": "2026-06-24T19:45:00.000Z"},
                    "competition": {"name": comp}, "marketCount": 30})
    return out


def fake_list_outright_markets(event_type_id, session, max_results=200):
    return _outright_markets_for(event_type_id)


def fake_list_market_types_for_events(event_ids, session):
    types = []
    for eid in event_ids:
        if eid not in _EVENTS:
            continue
        for m in _markets_for(eid):
            t = m["description"]["marketType"]
            if t not in types:
                types.append(t)
    return types


def fake_list_markets_for_events(event_ids, session, market_type_codes=None, max_results=100):
    out = []
    for eid in event_ids:
        if eid not in _EVENTS:
            continue
        for m in _markets_for(eid):
            if market_type_codes and m["description"]["marketType"] not in market_type_codes:
                continue
            out.append(m)
    out.sort(key=lambda m: m["totalMatched"], reverse=True)
    return out


def fake_list_market_books(market_ids, session):
    index = {m["marketId"]: m for eid in _EVENTS for m in _markets_for(eid)}
    for etid in _OUTRIGHTS:
        for m in _outright_markets_for(etid):
            index[m["marketId"]] = m
    books = []
    for mid in market_ids:
        m = index.get(mid)
        if not m:
            continue
        books.append({"marketId": mid, "status": "OPEN", "runners": [
            {"selectionId": r["selectionId"], "status": "ACTIVE", "handicap": 0.0,
             "ex": {"availableToBack": [{"price": _price_for(r["selectionId"]), "size": 500}],
                    "availableToLay": [{"price": _price_for(r["selectionId"]) + 0.04, "size": 400}]}}
            for r in m["runners"]]})
    return books


class SpyLLM:
    """Wraps the real client and records the tool calls the model makes per round."""

    def __init__(self, inner):
        self.inner = inner
        self.tool_sequence = []

    def complete(self, system, messages, tools=None, temperature=0):
        resp = self.inner.complete(system, messages, tools=tools, temperature=temperature)
        self.tool_sequence.append([tc.name for tc in resp.tool_calls] or ["<text>"])
        return resp


QUERIES = [
    "show me some markets in the England game",     # focused, no type   → expect NO price_markets
    "over/under 2.5 for Arsenal",                   # focused, typed     → expect NO price_markets
    "what football is on",                          # broad              → expect events list
    "England markets with odds better than 2.0",    # price filter       → expect price_markets
    "England",                                       # participant        → expect find_outrights + a "to win" card
    "Scottie Scheffler",                             # individual (golf)  → expect find_outrights (winner + top-10)
    "show me the odds for England to win the world cup",  # explicit outright → expect find_outrights WINNER
    "Scottie Scheffler top 10 finish",               # specific non-winner → expect TOP_10_FINISH card
    "will England reach the World Cup final",        # specific non-winner → expect TO_REACH_FINAL card
]


def run_one(query):
    spy = SpyLLM(OpenAIClient(model=AGENT_MODEL))
    # event_type_id_for is the real (pure) lookup so Football→1 / Golf→3 route to
    # the right fixtures and outrights; only the Betfair network calls are mocked.
    patches = [
        patch.object(search_agent, "get_llm", return_value=spy),
        patch.object(search_tools, "list_events_filtered", side_effect=fake_list_events_filtered),
        patch.object(search_tools, "list_outright_markets", side_effect=fake_list_outright_markets),
        patch.object(search_tools, "list_market_types_for_events", side_effect=fake_list_market_types_for_events),
        patch.object(search_tools, "list_markets_for_events", side_effect=fake_list_markets_for_events),
        patch.object(search_tools, "list_market_books", side_effect=fake_list_market_books),
    ]
    for p in patches:
        p.start()
    try:
        return spy, SearchAgent.run(query, {}, history=[])
    finally:
        for p in patches:
            p.stop()


if __name__ == "__main__":
    for q in QUERIES:
        spy, res = run_one(q)
        m = res["metrics"]
        called_price = any("price_markets" in step for step in spy.tool_sequence)
        called_outrights = any("find_outrights" in step for step in spy.tool_sequence)
        print("=" * 84)
        print(f"QUERY: {q}")
        print(f"  tool sequence : {spy.tool_sequence}")
        print(f"  rounds={m['rounds']}  price_calls={m['price_calls']}  cards={m['cards']}  "
              f"events={m['events']}  salvaged={m['salvaged']}")
        print(f"  >>> called price_markets as a round? {called_price}")
        print(f"  >>> called find_outrights?            {called_outrights}")
        print(f"  reply: {res['reply']!r}")
        for c in res["cards"][:5]:
            print(f"    - {c['event_name']} | {c['market_type']} | {c['runner_name']} @ {c['price']}")
        for e in (res.get("events") or [])[:5]:
            print(f"    o event {e['event_id']} {e['name']}")
    print("=" * 84)
