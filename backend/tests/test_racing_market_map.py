"""Tests for racing_market_for — the parsed market_type -> Betfair market mapping.

The behaviour under test replaced `RACING_MARKET_MAP.get(label, "WIN")`, which
turned any label it didn't recognise into a WIN bet. The audit of the real parse
log found two live examples ("Shallow top 3 in york 1 pound" and "1 euro on Harry
mole not to be top 3 Horse racing", both parsed as TOP_3_FINISH), i.e. real
requests for a top-3 finish that would have been placed as money on the horse to
win outright.
"""
import pytest

from backend.schemas.bets import ParsedBet
from backend.services.racing_service import (
    UnsupportedRacingMarketError,
    racing_market_for,
)


class TestWinSynonyms:
    @pytest.mark.parametrize("label", ["WIN", "MATCH_ODDS", "OUTRIGHT_WINNER", "WINNER"])
    def test_win_labels_map_to_win_with_no_places(self, label):
        assert racing_market_for(label) == ("WIN", None)

    def test_missing_label_defaults_to_win(self):
        assert racing_market_for(None) == ("WIN", None)

    def test_label_is_case_and_space_insensitive(self):
        assert racing_market_for("  each_way  ") == ("EACH_WAY", None)


class TestPlaceFamily:
    @pytest.mark.parametrize("label", ["PLACE", "TO_BE_PLACED"])
    def test_plain_place_keeps_places_none(self, label):
        assert racing_market_for(label) == ("PLACE", None)

    def test_explicit_places_are_preserved(self):
        assert racing_market_for("PLACE", 4) == ("PLACE", 4)

    @pytest.mark.parametrize("label,expected", [
        ("TOP_3_FINISH", 3),
        ("TOP_2_FINISH", 2),
        ("TOP_10_FINISH", 10),
        ("TOP_4", 4),
    ])
    def test_top_n_becomes_a_place_bet_paying_n(self, label, expected):
        # The regression this whole change exists for.
        assert racing_market_for(label) == ("PLACE", expected)

    def test_parser_supplied_places_beat_the_code(self):
        # If the parser worked out the count from the text, trust it over the
        # market-type label.
        assert racing_market_for("TOP_3_FINISH", 4) == ("PLACE", 4)

    def test_countless_top_n_is_a_plain_place_bet(self):
        assert racing_market_for("TOP_N_FINISH") == ("PLACE", None)


class TestDeclines:
    @pytest.mark.parametrize("label", ["FORECAST", "REV_FORECAST", "TRICAST", "MATCH_BET"])
    def test_known_unsupported_markets_keep_their_tailored_message(self, label):
        with pytest.raises(UnsupportedRacingMarketError) as exc:
            racing_market_for(label)
        assert "aren't supported" in str(exc.value) or "isn't supported" in str(exc.value)

    def test_unrecognised_label_is_declined_not_silently_placed_as_win(self):
        with pytest.raises(UnsupportedRacingMarketError) as exc:
            racing_market_for("SOME_FUTURE_MARKET")
        assert "SOME_FUTURE_MARKET" in str(exc.value)

    def test_a_golf_style_label_does_not_become_a_win_bet(self):
        # MAKE_CUT is meaningless for racing; the old default would have placed a
        # win bet on the horse.
        with pytest.raises(UnsupportedRacingMarketError):
            racing_market_for("MAKE_CUT")


def test_top_3_request_reaches_the_place_path_end_to_end():
    """The logged failure, exercised through resolve_racing_markets."""
    from unittest.mock import patch

    from backend.services import racing_service

    market = {
        "marketId": "1.1",
        "marketName": "To Be Placed",
        "marketStartTime": "2026-06-12T14:30:00Z",
        "description": {"marketType": "PLACE"},
        "event": {"id": "E1", "name": "York 12th Jun"},
        "runners": [{"selectionId": 11, "runnerName": "Shallow"}],
    }
    bet = ParsedBet(
        selection_name="Shallow", sport="Horse Racing", side="BACK", stake=1.0,
        market_type="TOP_3_FINISH", places=None,
    )

    with patch.object(racing_service, "list_racing_markets", return_value=[market]) as scan, \
         patch.object(racing_service, "list_place_markets_for_event", return_value=[market]), \
         patch.object(racing_service, "get_market_winners", return_value={"1.1": 3}):
        matches = racing_service.resolve_racing_markets(bet, "Shallow top 3 in york 1 pound", {})

    # A PLACE market was scanned for, not a WIN market.
    assert scan.call_args[0][1] == "PLACE"
    assert len(matches) == 1
    assert matches[0]["marketType"] == "PLACE"
    assert matches[0]["places"] == 3
