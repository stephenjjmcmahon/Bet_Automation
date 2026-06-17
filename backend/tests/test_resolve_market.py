"""Tests for the book-aware market resolver (search_service.resolve_market).

Covers the two limitations the resolver was hardened against:
  1. Inactive (REMOVED/withdrawn) runners that still appear in the catalogue must
     not be returned — re-pick among the live ACTIVE runners instead.
  2. A market type that spans several markets (differing only by line) must be
     reachable beyond the first one.
"""
from unittest.mock import patch
import pytest

from backend.schemas.bets import ParsedBet
from backend.services import search_service


def _bet(selection_name, market_type="MATCH_ODDS", line=None):
    return ParsedBet(
        selection_name=selection_name,
        sport="Test",
        side="BACK",
        stake=5.0,
        market_type=market_type,
        line=line,
    )


def _runner(sid, name, handicap=0.0):
    return {"selectionId": sid, "runnerName": name, "handicap": handicap}


def _market(market_id, runners, name="", market_type="MATCH_ODDS"):
    return {
        "marketId": market_id,
        "marketName": name,
        "runners": runners,
        "competition": {"name": "Test Comp"},
        "description": {"marketType": market_type},
    }


def _book(market_id, runner_statuses):
    """runner_statuses: list of (selectionId, status)."""
    return {
        "marketId": market_id,
        "status": "OPEN",
        "runners": [
            {"selectionId": sid, "status": st, "handicap": 0.0,
             "ex": {"availableToBack": [{"price": 2.0, "size": 100.0}], "availableToLay": []}}
            for sid, st in runner_statuses
        ],
    }


def _resolve(markets, book, parsed_bet, market_type="MATCH_ODDS", user_input=""):
    with patch.object(search_service, "list_market_catalogue", return_value=markets), \
         patch.object(search_service, "get_market_book", return_value=book):
        return search_service.resolve_market("evt1", parsed_bet, {}, market_type, user_input)


# --- happy path -------------------------------------------------------------

def test_simple_name_match_returns_runner_and_book():
    markets = [_market("1.1", [_runner(1, "Team A"), _runner(2, "Team B"), _runner(3, "Draw")])]
    book = _book("1.1", [(1, "ACTIVE"), (2, "ACTIVE"), (3, "ACTIVE")])
    out = _resolve(markets, book, _bet("Team A"))
    assert out["marketId"] == "1.1"
    assert out["selectionId"] == 1
    assert out["runnerName"] == "Team A"
    assert out["competition"] == "Test Comp"
    assert out["book"] is book  # threaded through so pricing doesn't refetch


# --- Limitation 1: inactive runners --------------------------------------------

def test_removed_runner_with_active_duplicate_is_repicked():
    # Catalogue lists a stale "Tyson Fury by KO/TKO or DQ" that out-matches the
    # live "Fury TKO/KO/DQ" on name; the book marks the stale one REMOVED.
    markets = [_market("1.2", [
        _runner(10, "Tyson Fury by KO/TKO or DQ"),
        _runner(11, "Fury TKO/KO/DQ"),
    ], market_type="METHOD_OF_VICTORY")]
    book = _book("1.2", [(10, "REMOVED"), (11, "ACTIVE")])
    with patch.object(search_service.AIInterpreter, "select_market_runner", return_value=11) as ai:
        out = _resolve(markets, book, _bet("Tyson Fury by KO", "METHOD_OF_VICTORY"),
                       market_type="METHOD_OF_VICTORY")
    assert out["selectionId"] == 11
    assert out["runnerName"] == "Fury TKO/KO/DQ"
    ai.assert_called_once()  # fallback used only for the active re-pick


def test_only_match_is_removed_and_no_active_alternative_raises():
    markets = [_market("1.3", [_runner(5, "Dobbin"), _runner(6, "Other Horse")])]
    book = _book("1.3", [(5, "REMOVED"), (6, "ACTIVE")])
    with patch.object(search_service.AIInterpreter, "select_market_runner", return_value=None):
        with pytest.raises(ValueError, match="not an active runner"):
            _resolve(markets, book, _bet("Dobbin", "WIN"), market_type="WIN")


# --- Limitation 2: multiple markets of one type --------------------------------

def test_line_selects_the_matching_alternate_market():
    # Two WINNING_MARGIN markets; the bet's line must reach the second one.
    m24 = _market("1.24", [_runner(1, "Geelong over 24.5pts"), _runner(2, "Draw")],
                  name="Winning Margin 24.5", market_type="WINNING_MARGIN")
    m39 = _market("1.39", [_runner(3, "Geelong over 39.5pts"), _runner(4, "Draw")],
                  name="Winning Margin 39.5", market_type="WINNING_MARGIN")
    book = _book("1.39", [(3, "ACTIVE"), (4, "ACTIVE")])
    out = _resolve([m24, m39], book, _bet("Geelong", "WINNING_MARGIN", line=39.5),
                   market_type="WINNING_MARGIN")
    assert out["marketId"] == "1.39"
    assert out["selectionId"] == 3


def test_ai_fallback_spans_all_markets():
    # No catalogue substring match anywhere; the AI fallback picks a runner that
    # lives in the *second* market, which must also select that market.
    m1 = _market("1.a", [_runner(1, "Hurricanes 8-14")], market_type="WINNING_MARGIN")
    m2 = _market("1.b", [_runner(2, "Hurricanes 15+")], market_type="WINNING_MARGIN")
    book = _book("1.b", [(2, "ACTIVE")])
    with patch.object(search_service.AIInterpreter, "select_market_runner", return_value=2):
        out = _resolve([m1, m2], book, _bet("Hurricanes by 15 or more", "WINNING_MARGIN"),
                       market_type="WINNING_MARGIN")
    assert out["marketId"] == "1.b"
    assert out["selectionId"] == 2


# --- _LINE markets ------------------------------------------------------------

def test_line_suffix_market_takes_first_runner():
    markets = [_market("1.l", [_runner(99, "Over"), _runner(99, "Under")],
                       market_type="1_INNING_20_OVR_LINE")]
    book = _book("1.l", [(99, "ACTIVE")])
    out = _resolve(markets, book, _bet("Over", "1_INNING_20_OVR_LINE"),
                   market_type="1_INNING_20_OVR_LINE")
    assert out["selectionId"] == 99
    assert out["book"] is book
