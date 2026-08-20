from unittest.mock import MagicMock, patch

from backend.schemas.bets import ParsedBet
from backend.services.ai_interpreter import AIInterpreter


def _mock_openai_response(content: str):
    """Build a minimal mock that looks like an OpenAI chat completion response."""
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


CANDIDATES = [
    {"event": {"id": "111", "name": "Arsenal v Burnley", "openDate": "2026-05-18T15:00:00Z"}},
    {"event": {"id": "222", "name": "Arsenal v Anderlecht", "openDate": "2026-05-22T19:45:00Z"}},
    {"event": {"id": "333", "name": "Arsenal Women v Burnley Women", "openDate": "2026-05-18T12:00:00Z"}},
]

MARKET_TYPES = ["MATCH_ODDS", "OVER_UNDER_25", "CORRECT_SCORE"]


def _selection(event_id: str, market_type: str = "MATCH_ODDS") -> dict:
    return {"event_id": event_id, "market_type": market_type}


# --- select_top_events ---

def test_selects_correct_event_from_candidates():
    mock_resp = _mock_openai_response('{"selections": [{"event_id": "111", "market_type": "MATCH_ODDS"}]}')
    with patch("backend.services.ai_interpreter._client.chat.completions.create", return_value=mock_resp):
        result = AIInterpreter.select_top_events("Back Arsenal vs Burnley 10", CANDIDATES, MARKET_TYPES)
    assert result == [_selection("111")]


def test_returns_empty_for_empty_candidates():
    # Should short-circuit before calling OpenAI at all
    with patch("backend.services.ai_interpreter._client.chat.completions.create") as mock_api:
        result = AIInterpreter.select_top_events("Back Arsenal 10", [], MARKET_TYPES)
    assert result == []
    mock_api.assert_not_called()


def test_returns_empty_when_ai_finds_no_match():
    mock_resp = _mock_openai_response('{"selections": []}')
    with patch("backend.services.ai_interpreter._client.chat.completions.create", return_value=mock_resp):
        result = AIInterpreter.select_top_events("Back Arsenal in the FA Cup Final 10", CANDIDATES, MARKET_TYPES)
    assert result == []


def test_returns_empty_when_ai_returns_empty_content():
    mock_resp = _mock_openai_response(None)
    with patch("backend.services.ai_interpreter._client.chat.completions.create", return_value=mock_resp):
        result = AIInterpreter.select_top_events("Back Arsenal 10", CANDIDATES, MARKET_TYPES)
    assert result == []


def test_drops_selections_missing_a_field():
    """A selection is only usable with both an event id and a market type."""
    mock_resp = _mock_openai_response(
        '{"selections": [{"event_id": "111"}, {"market_type": "MATCH_ODDS"}, '
        '{"event_id": "222", "market_type": "MATCH_ODDS"}]}'
    )
    with patch("backend.services.ai_interpreter._client.chat.completions.create", return_value=mock_resp):
        result = AIInterpreter.select_top_events("Back Arsenal 10", CANDIDATES, MARKET_TYPES)
    assert result == [_selection("222")]


def test_returns_multiple_ranked_events():
    mock_resp = _mock_openai_response(
        '{"selections": [{"event_id": "111", "market_type": "MATCH_ODDS"}, '
        '{"event_id": "333", "market_type": "MATCH_ODDS"}]}'
    )
    with patch("backend.services.ai_interpreter._client.chat.completions.create", return_value=mock_resp):
        result = AIInterpreter.select_top_events("Back Arsenal vs Burnley 10", CANDIDATES, MARKET_TYPES)
    assert result == [_selection("111"), _selection("333")]


def test_all_candidates_are_included_in_prompt():
    """Verify the AI prompt contains all candidate names so it can compare them."""
    mock_resp = _mock_openai_response('{"selections": [{"event_id": "111", "market_type": "MATCH_ODDS"}]}')
    with patch("backend.services.ai_interpreter._client.chat.completions.create", return_value=mock_resp) as mock_api:
        AIInterpreter.select_top_events("Back Arsenal vs Burnley 10", CANDIDATES, MARKET_TYPES)

    prompt = mock_api.call_args[1]["messages"][1]["content"]
    assert "Arsenal v Burnley" in prompt
    assert "Arsenal v Anderlecht" in prompt
    assert "Arsenal Women v Burnley Women" in prompt


def test_available_market_types_are_included_in_prompt():
    mock_resp = _mock_openai_response('{"selections": [{"event_id": "111", "market_type": "MATCH_ODDS"}]}')
    with patch("backend.services.ai_interpreter._client.chat.completions.create", return_value=mock_resp) as mock_api:
        AIInterpreter.select_top_events("Back Arsenal vs Burnley 10", CANDIDATES, MARKET_TYPES)

    prompt = mock_api.call_args[1]["messages"][1]["content"]
    for market_type in MARKET_TYPES:
        assert market_type in prompt


def test_user_input_is_included_in_prompt():
    mock_resp = _mock_openai_response('{"selections": [{"event_id": "111", "market_type": "MATCH_ODDS"}]}')
    with patch("backend.services.ai_interpreter._client.chat.completions.create", return_value=mock_resp) as mock_api:
        AIInterpreter.select_top_events("Back Arsenal vs Burnley tonight 10", CANDIDATES, MARKET_TYPES)

    prompt = mock_api.call_args[1]["messages"][1]["content"]
    assert "Back Arsenal vs Burnley tonight 10" in prompt


# --- market-type pin ---
#
# The gpt-4o parser applies the full market-type rules, so when it emits a code
# Betfair actually offers, that code wins over the lighter event-picker's choice.

def _parsed_bet(market_type: str) -> ParsedBet:
    return ParsedBet(
        selection_name="Arsenal",
        event_name="Arsenal",
        sport="Football",
        side="BACK",
        stake=10.0,
        market_type=market_type,
    )


def test_parsed_market_type_overrides_the_model_pick_when_offered():
    mock_resp = _mock_openai_response(
        '{"selections": [{"event_id": "111", "market_type": "MATCH_ODDS"}]}'
    )
    with patch("backend.services.ai_interpreter._client.chat.completions.create", return_value=mock_resp):
        result = AIInterpreter.select_top_events(
            "correct score Arsenal 2-0 vs Burnley 10",
            CANDIDATES,
            MARKET_TYPES,
            parsed_bet=_parsed_bet("CORRECT_SCORE"),
        )
    assert result == [_selection("111", "CORRECT_SCORE")]


def test_model_pick_survives_when_parsed_market_type_is_not_offered():
    """OUTRIGHT_WINNER is the parser's internal label; football's Betfair code is
    WINNER, so an unoffered parsed code must not be pinned over a valid pick."""
    mock_resp = _mock_openai_response(
        '{"selections": [{"event_id": "111", "market_type": "MATCH_ODDS"}]}'
    )
    with patch("backend.services.ai_interpreter._client.chat.completions.create", return_value=mock_resp):
        result = AIInterpreter.select_top_events(
            "Arsenal to win the Premier League 10",
            CANDIDATES,
            MARKET_TYPES,
            parsed_bet=_parsed_bet("OUTRIGHT_WINNER"),
        )
    assert result == [_selection("111", "MATCH_ODDS")]
