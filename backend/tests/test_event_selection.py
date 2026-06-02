import pytest
from unittest.mock import MagicMock, patch
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


# --- select_top_events ---

def test_selects_correct_event_from_candidates():
    mock_resp = _mock_openai_response('{"event_ids": ["111"]}')
    with patch("backend.services.ai_interpreter.client.chat.completions.create", return_value=mock_resp):
        result = AIInterpreter.select_top_events("Back Arsenal vs Burnley 10", CANDIDATES)
    assert result == ["111"]


def test_returns_empty_for_empty_candidates():
    # Should short-circuit before calling OpenAI at all
    with patch("backend.services.ai_interpreter.client.chat.completions.create") as mock_api:
        result = AIInterpreter.select_top_events("Back Arsenal 10", [])
    assert result == []
    mock_api.assert_not_called()


def test_returns_empty_when_ai_finds_no_match():
    mock_resp = _mock_openai_response('{"event_ids": []}')
    with patch("backend.services.ai_interpreter.client.chat.completions.create", return_value=mock_resp):
        result = AIInterpreter.select_top_events("Back Arsenal in the FA Cup Final 10", CANDIDATES)
    assert result == []


def test_returns_empty_when_ai_returns_empty_content():
    mock_resp = _mock_openai_response(None)
    with patch("backend.services.ai_interpreter.client.chat.completions.create", return_value=mock_resp):
        result = AIInterpreter.select_top_events("Back Arsenal 10", CANDIDATES)
    assert result == []


def test_returns_multiple_ranked_events():
    mock_resp = _mock_openai_response('{"event_ids": ["111", "333"]}')
    with patch("backend.services.ai_interpreter.client.chat.completions.create", return_value=mock_resp):
        result = AIInterpreter.select_top_events("Back Arsenal vs Burnley 10", CANDIDATES)
    assert result == ["111", "333"]


def test_all_candidates_are_included_in_prompt():
    """Verify the AI prompt contains all candidate names so it can compare them."""
    mock_resp = _mock_openai_response('{"event_ids": ["111"]}')
    with patch("backend.services.ai_interpreter.client.chat.completions.create", return_value=mock_resp) as mock_api:
        AIInterpreter.select_top_events("Back Arsenal vs Burnley 10", CANDIDATES)

    prompt = mock_api.call_args[1]["messages"][1]["content"]
    assert "Arsenal v Burnley" in prompt
    assert "Arsenal v Anderlecht" in prompt
    assert "Arsenal Women v Burnley Women" in prompt


def test_user_input_is_included_in_prompt():
    mock_resp = _mock_openai_response('{"event_ids": ["111"]}')
    with patch("backend.services.ai_interpreter.client.chat.completions.create", return_value=mock_resp) as mock_api:
        AIInterpreter.select_top_events("Back Arsenal vs Burnley tonight 10", CANDIDATES)

    prompt = mock_api.call_args[1]["messages"][1]["content"]
    assert "Back Arsenal vs Burnley tonight 10" in prompt
