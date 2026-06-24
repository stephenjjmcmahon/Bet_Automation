"""Tests for the search agent's intent classifier and card integrity gate.

The LLM is mocked (no network). The card gate is a pure function over collected
price_markets output — the guarantee that the agent can only surface real,
priced selections.
"""
from unittest.mock import patch

from backend.services import search_agent, search_tools
from backend.services.search_agent import SearchAgent
from backend.services.search_tools import SearchTools
from backend.services.llm import LLMResponse


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
