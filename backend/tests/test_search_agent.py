"""Tests for the search agent's intent classifier and card integrity gate.

The LLM is mocked (no network). The card gate is a pure function over collected
price_markets output — the guarantee that the agent can only surface real,
priced selections.
"""
from contextlib import ExitStack
from unittest.mock import patch

from backend.services import search_agent, search_tools
from backend.services.llm import LLMResponse, ToolCall
from backend.services.search_agent import SearchAgent
from backend.services.search_tools import SearchTools


class FakeLLM:
    def __init__(self, text):
        self._text = text

    def complete(self, system, messages, tools=None, temperature=0):
        return LLMResponse(text=self._text)


def test_classify_intent_search():
    with patch.object(search_agent, "get_llm", return_value=FakeLLM("search")):
        assert search_agent.classify_intent("show me the world cup markets") == "search"


def test_classify_intent_bet():
    with patch.object(search_agent, "get_llm", return_value=FakeLLM("bet")):
        assert search_agent.classify_intent("back Arsenal £20") == "bet"


def test_classify_intent_defaults_to_bet_when_unclear():
    with patch.object(search_agent, "get_llm", return_value=FakeLLM("not sure")):
        assert search_agent.classify_intent("???") == "bet"


def test_build_cards_drops_unpriced_market_and_uses_server_data():
    priced_cache = {
        "1.1": {
            "market_id": "1.1", "market_name": "Match Odds", "market_type": "MATCH_ODDS",
            "event_id": "e1", "event_name": "A v B", "competition": "C",
            "market_start_time": "2026-06-17T19:00:00Z",
            "runners": [
                {"selection_id": 1, "runner_name": "A", "handicap": 0,
                 "back_price": 1.8, "back_size": 100, "lay_price": 1.85, "lay_size": 50},
                {"selection_id": 2, "runner_name": "B", "handicap": 0,
                 "back_price": None, "back_size": None, "lay_price": None, "lay_size": None},
            ],
        }
    }
    # The agent names a real market and a hallucinated one.
    args = {"markets": [{"market_id": "1.1"}, {"market_id": "ghost"}]}
    cards = SearchAgent._build_cards(args, priced_cache)

    # ghost market dropped; runner B (no backable price) dropped.
    assert len(cards) == 1
    assert cards[0]["selection_id"] == 1
    assert cards[0]["price"] == 1.8          # taken from server data, not the model
    assert cards[0]["event_name"] == "A v B"


def test_build_event_cards_uses_find_events_cache_and_gates_unknown():
    from backend.services.search_tools import SearchTools
    tools = SearchTools({})
    # Simulate find_events having cached two events.
    tools._events = {
        "e1": {"event_id": "e1", "name": "Ascot", "open_date": "2026-06-18T13:30:00Z", "competition": None},
        "e2": {"event_id": "e2", "name": "York", "open_date": "2026-06-18T14:00:00Z", "competition": None},
    }
    args = {"events": [{"event_id": "e1"}, {"event_id": "ghost"}, {"event_id": "e2"}]}
    cards = SearchAgent._build_event_cards(args, tools)
    assert [c["event_id"] for c in cards] == ["e1", "e2"]  # ghost dropped
    assert cards[0]["name"] == "Ascot"


def _priced_cache_one():
    return {
        "1.1": {
            "market_id": "1.1", "market_name": "Total Points", "market_type": "TOTAL_MATCH_POINTS",
            "event_id": "e1", "event_name": "Fremantle v Geelong", "competition": "AFL",
            "market_start_time": "t",
            "runners": [
                {"selection_id": 5, "runner_name": "150 Points Or Less", "handicap": 0,
                 "back_price": 3.1, "back_size": 50, "lay_price": 3.3, "lay_size": 20},
            ],
        }
    }


def test_result_salvages_priced_cache_when_model_narrates_in_prose():
    # Model ended in prose (no cards/events) but markets were priced -> surface them.
    res = SearchAgent._result("Here are the total points markets: ...", [], [], SearchTools({}), _priced_cache_one())
    assert len(res["cards"]) == 1
    assert res["cards"][0]["selection_id"] == 5
    assert res["cards"][0]["price"] == 3.1
    assert res["reply"].startswith("Here are the total points")


def test_result_does_not_override_explicit_cards():
    explicit = [{"selection_id": 99}]
    res = SearchAgent._result("hi", explicit, [], SearchTools({}), _priced_cache_one())
    assert res["cards"] == explicit  # no salvage when something was already presented


def test_result_auto_prices_listed_markets_when_model_skipped_pricing():
    # Model listed a market but never priced/presented it -> server prices on exit.
    tools = SearchTools({})
    cat = {
        "marketId": "2.2", "marketName": "Total Match Points", "totalMatched": 100,
        "description": {"marketType": "TOTAL_MATCH_POINTS"},
        "event": {"id": "e1", "name": "Fremantle v Geelong"}, "competition": {"name": "AFL"},
        "marketStartTime": "t",
        "runners": [{"selectionId": 7, "runnerName": "150 Or Less", "handicap": 0.0}],
    }
    with patch.object(search_tools, "list_markets_for_events", return_value=[cat]):
        tools.list_markets(["e1"])
    book = {"marketId": "2.2", "status": "OPEN", "runners": [
        {"selectionId": 7, "status": "ACTIVE", "handicap": 0.0,
         "ex": {"availableToBack": [{"price": 3.1, "size": 50}], "availableToLay": []}},
    ]}
    with patch.object(search_tools, "list_market_books", return_value=[book]):
        res = SearchAgent._result("prose only", [], [], tools, {})
    assert len(res["cards"]) == 1
    assert res["cards"][0]["price"] == 3.1


def test_build_cards_respects_selection_filter():
    priced_cache = {
        "1.1": {
            "market_id": "1.1", "market_name": "O/U 2.5", "market_type": "OVER_UNDER_25",
            "event_id": "e1", "event_name": "A v B", "competition": "C",
            "market_start_time": "t",
            "runners": [
                {"selection_id": 10, "runner_name": "Over 2.5", "handicap": 0,
                 "back_price": 1.95, "back_size": 400, "lay_price": 1.98, "lay_size": 300},
                {"selection_id": 11, "runner_name": "Under 2.5", "handicap": 0,
                 "back_price": 1.97, "back_size": 380, "lay_price": 2.0, "lay_size": 200},
            ],
        }
    }
    args = {"markets": [{"market_id": "1.1", "selection_ids": [10]}]}
    cards = SearchAgent._build_cards(args, priced_cache)
    assert [c["selection_id"] for c in cards] == [10]


# ── Full-loop tests for the pricing fold ────────────────────────────────────────
# These drive SearchAgent.run end to end with a scripted tool-calling LLM and a
# mocked Betfair, to prove the fold: the model presents markets WITHOUT a separate
# price round, and present_results prices them on exit.

class ScriptedLLM:
    """Returns pre-scripted LLMResponses in order, one per complete() call. Raises
    IndexError if the loop asks for more rounds than scripted (a useful signal)."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = 0

    def complete(self, system, messages, tools=None, temperature=0):
        self.calls += 1
        return self._responses.pop(0)


def _tc(name, args, id="c"):
    return ToolCall(id=id, name=name, arguments=args)


def _events_fixture():
    return [{"event": {"id": "33941409", "name": "Arsenal v Chelsea",
                       "openDate": "2026-06-24T19:45:00.000Z"}, "marketCount": 38}]


def _catalogue_fixture():
    return [{
        "marketId": "1.240918337", "marketName": "Over/Under 2.5 Goals",
        "totalMatched": 486120.5, "description": {"marketType": "OVER_UNDER_25"},
        "event": {"id": "33941409", "name": "Arsenal v Chelsea"},
        "competition": {"name": "English Premier League"},
        "marketStartTime": "2026-06-24T19:45:00.000Z",
        "runners": [
            {"selectionId": 47973, "runnerName": "Over 2.5 Goals", "handicap": 0.0},
            {"selectionId": 47972, "runnerName": "Under 2.5 Goals", "handicap": 0.0},
        ],
    }]


def _book_fixture():
    return [{"marketId": "1.240918337", "status": "OPEN", "runners": [
        {"selectionId": 47973, "status": "ACTIVE", "handicap": 0.0,
         "ex": {"availableToBack": [{"price": 2.02, "size": 1240}],
                "availableToLay": [{"price": 2.04, "size": 910}]}},
        {"selectionId": 47972, "status": "ACTIVE", "handicap": 0.0,
         "ex": {"availableToBack": [{"price": 1.96, "size": 1500}],
                "availableToLay": [{"price": 1.98, "size": 1330}]}},
    ]}]


def _run_with(scripted, book_calls):
    """Run SearchAgent.run with the scripted LLM and a mocked Betfair. `book_calls`
    collects each list_market_books call's ids so a test can assert how many times
    (and with what) pricing actually happened."""
    def fake_books(ids, session):
        book_calls.append(list(ids))
        known = {b["marketId"]: b for b in _book_fixture()}
        return [known[i] for i in ids if i in known]

    with ExitStack() as stack:
        stack.enter_context(patch.object(search_agent, "get_llm", return_value=scripted))
        stack.enter_context(patch.object(search_tools, "list_events_filtered", return_value=_events_fixture()))
        stack.enter_context(patch.object(search_tools, "list_markets_for_events", return_value=_catalogue_fixture()))
        stack.enter_context(patch.object(search_tools, "list_market_books", side_effect=fake_books))
        return SearchAgent.run("over/under 2.5 for Arsenal", {}, history=[])


def test_present_prices_on_exit_without_a_price_round():
    # The fold: find_events → list_markets → present_results (NO price_markets call).
    # present_results must price the chosen market on exit and build real cards.
    book_calls = []
    scripted = ScriptedLLM(
        LLMResponse(tool_calls=[_tc("find_events", {"sport": "Football", "text": "Arsenal"}, "c1")]),
        LLMResponse(tool_calls=[_tc("list_markets", {"event_ids": ["33941409"], "market_types": ["OVER_UNDER_25"]}, "c2")]),
        LLMResponse(text="intro", tool_calls=[_tc("present_results", {
            "reply": "Here are the over/under 2.5 prices for Arsenal v Chelsea:",
            "markets": [{"market_id": "1.240918337"}],
        }, "c3")]),
    )
    res = _run_with(scripted, book_calls)

    # Three rounds, present reached without a dedicated price round.
    assert scripted.calls == 3
    assert res["metrics"]["rounds"] == 3
    assert res["metrics"]["salvaged"] is False
    # Pricing happened exactly once — on exit (the fold), not as its own round.
    assert res["metrics"]["price_calls"] == 1
    assert len(book_calls) == 1
    # Cards built from server price data (the integrity gate), both runners backable.
    by_sel = {c["selection_id"]: c for c in res["cards"]}
    assert by_sel[47973]["price"] == 2.02
    assert by_sel[47972]["price"] == 1.96
    assert by_sel[47973]["market_type"] == "OVER_UNDER_25"
    assert res["reply"].startswith("Here are the over/under")


def test_explicit_price_markets_path_still_works_and_does_not_double_price():
    # Price-filter queries still call price_markets explicitly; present_results must
    # then NOT re-price the already-cached market (no double pricing).
    book_calls = []
    scripted = ScriptedLLM(
        LLMResponse(tool_calls=[_tc("find_events", {"sport": "Football", "text": "Arsenal"}, "c1")]),
        LLMResponse(tool_calls=[_tc("list_markets", {"event_ids": ["33941409"]}, "c2")]),
        LLMResponse(tool_calls=[_tc("price_markets", {"market_ids": ["1.240918337"]}, "c3")]),
        LLMResponse(text="intro", tool_calls=[_tc("present_results", {
            "reply": "Best value:",
            "markets": [{"market_id": "1.240918337", "selection_ids": [47973]}],
        }, "c4")]),
    )
    res = _run_with(scripted, book_calls)

    assert res["metrics"]["rounds"] == 4
    # Priced once explicitly; exit-pricing must skip it (already in priced_cache).
    assert res["metrics"]["price_calls"] == 1
    assert len(book_calls) == 1
    # Selection filter respected, server price used.
    assert [c["selection_id"] for c in res["cards"]] == [47973]
    assert res["cards"][0]["price"] == 2.02


def test_present_drops_unlisted_market_keeps_real_one():
    # Integrity gate through the full loop: a presented market that was never listed
    # (no catalogue meta) cannot be priced, so it produces no card — while the real
    # listed market priced on exit still does.
    book_calls = []
    scripted = ScriptedLLM(
        LLMResponse(tool_calls=[_tc("find_events", {"sport": "Football", "text": "Arsenal"}, "c1")]),
        LLMResponse(tool_calls=[_tc("list_markets", {"event_ids": ["33941409"]}, "c2")]),
        LLMResponse(text="intro", tool_calls=[_tc("present_results", {
            "reply": "Here:",
            "markets": [{"market_id": "1.240918337"}, {"market_id": "9.999"}],
        }, "c3")]),
    )
    res = _run_with(scripted, book_calls)

    card_market_ids = {c["market_id"] for c in res["cards"]}
    assert "1.240918337" in card_market_ids   # real, listed → priced on exit
    assert "9.999" not in card_market_ids      # never listed → dropped by the gate


# ── Outright/participant search (find_outrights) ────────────────────────────────
# The participant ("England", "Scottie Scheffler") is a RUNNER inside a
# competition-winner market, never in the event name, so find_events can't see it.
# find_outrights scans winner-market runners (the racing pattern) and returns
# bettable rows that present_results prices on exit — no extra LLM round.

def _outright_catalogue():
    """Football winner markets: England is a runner in the World Cup and the Euros
    (named after the COMPETITION, not England), but not in Copa America."""
    def mk(mid, comp, total, runners):
        return {
            "marketId": mid, "marketName": "Winner", "totalMatched": total,
            "description": {"marketType": "WINNER"},
            "event": {"id": "ev" + mid, "name": comp}, "competition": {"name": comp},
            "marketStartTime": "2026-07-01T12:00:00.000Z",
            "runners": [{"selectionId": s, "runnerName": r, "handicap": 0.0} for s, r in runners],
        }
    return [
        mk("1.111", "FIFA World Cup", 900000, [(1, "England"), (2, "Brazil"), (3, "France")]),
        mk("1.222", "UEFA Euro 2028", 400000, [(11, "England"), (12, "Germany")]),
        mk("1.333", "Copa America", 50000, [(21, "Argentina"), (22, "Brazil")]),
    ]


def test_find_outrights_matches_runner_across_competitions_and_caches():
    tools = SearchTools({})
    with patch.object(search_tools, "event_type_id_for", return_value="1"), \
         patch.object(search_tools, "list_outright_markets", return_value=_outright_catalogue()):
        rows = tools.find_outrights("Football", "England")

    # England is a runner in the World Cup + Euros only; liquidity order preserved.
    assert [r["market_id"] for r in rows] == ["1.111", "1.222"]
    assert rows[0]["selection_id"] == 1 and rows[0]["runner_name"] == "England"
    assert rows[0]["event_name"] == "FIFA World Cup"
    assert rows[0]["market_type"] == "WINNER"
    # Matched markets are cached so present_results can price them / the gate accepts them.
    assert "1.111" in tools._markets and "1.222" in tools._markets
    assert "1.333" not in tools._markets


def test_find_outrights_surfaces_non_winner_markets_with_type():
    # Breadth: the scan is market-type agnostic, so a placing market (TOP_10_FINISH)
    # surfaces alongside the winner — each tagged with its market_type so the agent
    # can present the kind that fits the query ("to win" vs "top 10 finish").
    catalogue = [
        {"marketId": "1.1", "marketName": "Winner", "totalMatched": 500000,
         "description": {"marketType": "WINNER"}, "event": {"id": "e", "name": "The Open"},
         "competition": {"name": "The Open"}, "marketStartTime": "t",
         "runners": [{"selectionId": 5, "runnerName": "Scottie Scheffler", "handicap": 0.0}]},
        {"marketId": "1.2", "marketName": "Top 10 Finish", "totalMatched": 120000,
         "description": {"marketType": "TOP_10_FINISH"}, "event": {"id": "e", "name": "The Open"},
         "competition": {"name": "The Open"}, "marketStartTime": "t",
         "runners": [{"selectionId": 6, "runnerName": "Scottie Scheffler", "handicap": 0.0}]},
    ]
    tools = SearchTools({})
    with patch.object(search_tools, "event_type_id_for", return_value="3"), \
         patch.object(search_tools, "list_outright_markets", return_value=catalogue):
        rows = tools.find_outrights("Golf", "Scottie Scheffler")
    assert {r["market_type"] for r in rows} == {"WINNER", "TOP_10_FINISH"}
    assert all(r["runner_name"] == "Scottie Scheffler" for r in rows)


def test_outright_market_types_cover_broad_competition_set_not_fixtures():
    # Guard: the allowlist must stay BROAD (winners + placings + progression + group
    # + nationality + awards), and must NOT include per-fixture types (those are
    # served by the find_events path and would flood/duplicate the outright scan).
    from backend.services.betfair_client import OUTRIGHT_MARKET_TYPES
    for code in ["WINNER", "OUTRIGHT_WINNER", "TOURNAMENT_WINNER", "TOP_10_FINISH",
                 "MAKE_THE_CUT", "EACH_WAY", "TO_REACH_FINAL", "TO_QUALIFY",
                 "GROUP_A_WINNER", "TOP_NATIONALITY", "GOLDEN_BOOT", "TOP_GOALSCORER"]:
        assert code in OUTRIGHT_MARKET_TYPES
    for code in ["MATCH_ODDS", "OVER_UNDER_25", "CORRECT_SCORE", "BOTH_TEAMS_TO_SCORE", "HANDICAP"]:
        assert code not in OUTRIGHT_MARKET_TYPES


def test_find_outrights_uses_fuzzy_only_when_no_exact_match():
    tools = SearchTools({})
    catalogue = [{
        "marketId": "1.9", "marketName": "Winner", "totalMatched": 100,
        "description": {"marketType": "WINNER"}, "event": {"id": "e", "name": "The Open"},
        "competition": {"name": "The Open"}, "marketStartTime": "t",
        "runners": [{"selectionId": 5, "runnerName": "Scottie Scheffler", "handicap": 0.0},
                    {"selectionId": 6, "runnerName": "Rory McIlroy", "handicap": 0.0}],
    }]
    with patch.object(search_tools, "event_type_id_for", return_value="3"), \
         patch.object(search_tools, "list_outright_markets", return_value=catalogue):
        rows = tools.find_outrights("Golf", "Scheffler")   # partial name → substring fallback
    assert [r["selection_id"] for r in rows] == [5]
    assert rows[0]["runner_name"] == "Scottie Scheffler"


def test_find_outrights_caps_matches_and_handles_unsupported_sport():
    from backend.config.sport_mapping import UnsupportedSportError
    from backend.services.search_tools import MAX_OUTRIGHT_MATCHES
    n = MAX_OUTRIGHT_MATCHES + 5
    many = [{
        "marketId": f"1.{i}", "marketName": "Winner", "totalMatched": 1000 - i,
        "description": {"marketType": "WINNER"}, "event": {"id": f"e{i}", "name": f"Comp {i}"},
        "competition": {"name": f"Comp {i}"}, "marketStartTime": "t",
        "runners": [{"selectionId": i, "runnerName": "England", "handicap": 0.0}],
    } for i in range(n)]
    tools = SearchTools({})
    with patch.object(search_tools, "event_type_id_for", return_value="1"), \
         patch.object(search_tools, "list_outright_markets", return_value=many):
        rows = tools.find_outrights("Football", "England")
    assert len(rows) == MAX_OUTRIGHT_MATCHES    # capped
    # input is liquidity-ranked upstream; the cap keeps the most-traded (first) ones.
    assert [r["market_id"] for r in rows] == [f"1.{i}" for i in range(MAX_OUTRIGHT_MATCHES)]

    # Unsupported sport returns [] like find_events, never raises.
    with patch.object(search_tools, "event_type_id_for", side_effect=UnsupportedSportError("x")):
        assert SearchTools({}).find_outrights("Quidditch", "England") == []
    # Blank name short-circuits with no API call.
    assert SearchTools({}).find_outrights("Football", "  ") == []


def _outright_book_fixture():
    return [{"marketId": "1.111", "status": "OPEN", "runners": [
        {"selectionId": 1, "status": "ACTIVE", "handicap": 0.0,
         "ex": {"availableToBack": [{"price": 8.0, "size": 500}], "availableToLay": [{"price": 8.4, "size": 300}]}},
        {"selectionId": 2, "status": "ACTIVE", "handicap": 0.0,
         "ex": {"availableToBack": [{"price": 5.0, "size": 800}], "availableToLay": [{"price": 5.2, "size": 600}]}},
        {"selectionId": 3, "status": "ACTIVE", "handicap": 0.0,
         "ex": {"availableToBack": [{"price": 6.0, "size": 400}], "availableToLay": [{"price": 6.2, "size": 200}]}},
    ]}]


def test_find_outrights_present_builds_participant_card_on_exit():
    # "England to win the world cup": find_outrights → present_results(selection-filtered).
    # Two rounds, priced on exit, one England card built from server data.
    book_calls = []
    def fake_books(ids, session):
        book_calls.append(list(ids))
        known = {b["marketId"]: b for b in _outright_book_fixture()}
        return [known[i] for i in ids if i in known]

    scripted = ScriptedLLM(
        LLMResponse(tool_calls=[_tc("find_outrights", {"sport": "Football", "name": "England"}, "c1")]),
        LLMResponse(text="intro", tool_calls=[_tc("present_results", {
            "reply": "Here's England to win the World Cup:",
            "markets": [{"market_id": "1.111", "selection_ids": [1]}],
        }, "c2")]),
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(search_agent, "get_llm", return_value=scripted))
        stack.enter_context(patch.object(search_tools, "event_type_id_for", return_value="1"))
        stack.enter_context(patch.object(search_tools, "list_outright_markets", return_value=_outright_catalogue()))
        stack.enter_context(patch.object(search_tools, "list_market_books", side_effect=fake_books))
        res = SearchAgent.run("England to win the world cup", {}, history=[])

    assert scripted.calls == 2                  # find_outrights → present, no extra round
    assert res["metrics"]["rounds"] == 2
    assert res["metrics"]["price_calls"] == 1   # priced once, on exit (the fold)
    assert len(book_calls) == 1
    assert [c["selection_id"] for c in res["cards"]] == [1]   # only England, not Brazil/France
    assert res["cards"][0]["price"] == 8.0
    assert res["cards"][0]["runner_name"] == "England"
    assert res["cards"][0]["event_name"] == "FIFA World Cup"
    assert res["cards"][0]["market_type"] == "WINNER"
