from unittest.mock import patch

import pytest

from backend.schemas.bets import ParsedBet
from backend.services.racing_service import (
    RacingClarificationError,
    UnsupportedRacingMarketError,
    resolve_racing_markets,
)

SESSION = {"token": "fake"}


def _bet(selection="Constitution Hill", market_type="WIN", event_name=None, sport="Horse Racing", places=None):
    return ParsedBet(
        selection_name=selection,
        event_name=event_name,
        sport=sport,
        side="BACK",
        stake=20,
        market_type=market_type,
        places=places,
    )


def _market(market_id, meeting, race, runners, event_id="E1"):
    return {
        "marketId": market_id,
        "marketName": race,
        "marketStartTime": "2026-06-12T14:30:00.000Z",
        "event": {"id": event_id, "name": meeting},
        "runners": [{"selectionId": sid, "runnerName": name} for sid, name in runners],
    }


ASCOT_1430 = _market("1.111", "Ascot 12th Jun", "14:30 2m Hcap", [
    (101, "Constitution Hill"), (102, "Lunar Glow"),
])
ASCOT_1505 = _market("1.222", "Ascot 12th Jun", "15:05 1m Mdn", [
    (201, "Desert Crown"), (202, "Mystic Star"),
])
KEMPTON_1900 = _market("1.333", "Kempton 12th Jun", "19:00 6f Hcap", [
    (301, "Swift Dancer"), (302, "Night Runner"),
], event_id="E2")
ROMFORD_1930 = _market("1.444", "Romford 12th Jun", "19:30 A3", [
    (401, "Swift Dancer"), (402, "Slow Burner"),
], event_id="E3")


class TestTier1Matching:
    def test_single_match_resolves_the_race(self):
        with patch("backend.services.racing_service.list_racing_markets",
                   return_value=[ASCOT_1430, ASCOT_1505, KEMPTON_1900]):
            matches = resolve_racing_markets(_bet(), "back Constitution Hill 20", SESSION)
        assert len(matches) == 1
        assert matches[0]["marketId"] == "1.111"
        assert matches[0]["selectionId"] == 101
        assert matches[0]["runnerName"] == "Constitution Hill"
        assert matches[0]["marketType"] == "WIN"
        assert matches[0]["competition"] == "Ascot 12th Jun"

    def test_match_odds_label_maps_to_win_market_type(self):
        with patch("backend.services.racing_service.list_racing_markets",
                   return_value=[ASCOT_1430]) as mock_fetch:
            resolve_racing_markets(_bet(market_type="MATCH_ODDS"), "back Constitution Hill 20", SESSION)
        assert mock_fetch.call_args[0][1] == "WIN"

    def test_multiple_matches_return_a_slip_each(self):
        # Same dog name at two tracks → both come back, the user picks.
        with patch("backend.services.racing_service.list_racing_markets",
                   return_value=[KEMPTON_1900, ROMFORD_1930]):
            matches = resolve_racing_markets(_bet(selection="Swift Dancer"), "back Swift Dancer 20", SESSION)
        assert {m["marketId"] for m in matches} == {"1.333", "1.444"}

    def test_too_many_matches_without_meeting_asks_for_one(self):
        crowded = [
            _market(f"1.{i}", f"Track{i} 12th Jun", "14:00", [(500 + i, "Spirit Dancer")])
            for i in range(5)
        ]
        with patch("backend.services.racing_service.list_racing_markets", return_value=crowded):
            with pytest.raises(RacingClarificationError) as exc:
                resolve_racing_markets(_bet(selection="Spirit"), "back Spirit 20", SESSION)
        assert "meeting" in exc.value.question.lower()

    def test_no_match_without_meeting_asks_for_one(self):
        with patch("backend.services.racing_service.list_racing_markets",
                   return_value=[ASCOT_1430]):
            with pytest.raises(RacingClarificationError) as exc:
                resolve_racing_markets(_bet(selection="Unknown Horse"), "back Unknown Horse 20", SESSION)
        assert "meeting" in exc.value.question.lower()

    def test_exact_name_beats_substring_noise_in_full_scan(self):
        # 'Star' substring-matches many runners across the day, but one horse is
        # named exactly 'Star' — the exact match must win rather than blowing past
        # MAX_RACING_MATCHES and forcing a clarification.
        noisy = [
            _market("9.1", "Ascot", "14:00", [(901, "Star Gazer"), (902, "Lucky Star")]),
            _market("9.2", "Kempton", "14:05", [(903, "Star")], event_id="E9"),
            _market("9.3", "York", "14:10", [(904, "Northern Star"), (905, "Star Light")], event_id="E10"),
        ]
        with patch("backend.services.racing_service.list_racing_markets", return_value=noisy):
            matches = resolve_racing_markets(_bet(selection="Star"), "back Star 20", SESSION)
        assert len(matches) == 1
        assert matches[0]["marketId"] == "9.2"
        assert matches[0]["selectionId"] == 903

    def test_meeting_name_narrows_the_scan(self):
        with patch("backend.services.racing_service.list_racing_markets",
                   return_value=[KEMPTON_1900, ROMFORD_1930]):
            matches = resolve_racing_markets(
                _bet(selection="Swift Dancer", event_name="Romford", sport="Greyhound Racing"),
                "back Swift Dancer at Romford 20", SESSION,
            )
        assert len(matches) == 1
        assert matches[0]["marketId"] == "1.444"


class TestTier2AIPick:
    def test_no_exact_match_with_meeting_falls_back_to_ai(self):
        with patch("backend.services.racing_service.list_racing_markets",
                   return_value=[ASCOT_1430, ASCOT_1505, KEMPTON_1900]), \
             patch("backend.services.ai_interpreter.AIInterpreter.select_racing_runner",
                   return_value={"market_id": "1.111", "selection_id": 101}) as mock_pick:
            matches = resolve_racing_markets(
                _bet(selection="Konstitution Hil", event_name="Ascot"),
                "back Konstitution Hil at Ascot 20", SESSION,
            )
        assert len(matches) == 1
        assert matches[0]["selectionId"] == 101
        # The AI only ever sees the named meeting's races, not the full day.
        ai_markets = mock_pick.call_args[0][1]
        assert {m["marketId"] for m in ai_markets} == {"1.111", "1.222"}

    def test_ai_finding_nothing_asks_to_check_the_name(self):
        with patch("backend.services.racing_service.list_racing_markets",
                   return_value=[ASCOT_1430]), \
             patch("backend.services.ai_interpreter.AIInterpreter.select_racing_runner",
                   return_value=None):
            with pytest.raises(RacingClarificationError):
                resolve_racing_markets(
                    _bet(selection="Total Nonsense", event_name="Ascot"),
                    "back Total Nonsense at Ascot 20", SESSION,
                )


class TestUnsupportedMarkets:
    @pytest.mark.parametrize("market_type", ["FORECAST", "REV_FORECAST", "MATCH_BET", "RACE_WIN_DIST"])
    def test_unsupported_market_types_decline_cleanly(self, market_type):
        with pytest.raises(UnsupportedRacingMarketError):
            resolve_racing_markets(_bet(market_type=market_type), "whatever", SESSION)


# Native each-way market for a race (its own marketId + selectionIds).
ASCOT_1430_EW = _market("3.111", "Ascot 12th Jun", "14:30 2m Hcap", [
    (3101, "Constitution Hill"), (3102, "Lunar Glow"),
])


def _place_market(market_id, market_type, race_name, places_runners, event_id="E1"):
    m = _market(market_id, "Ascot 12th Jun", race_name, places_runners, event_id=event_id)
    m["description"] = {"marketType": market_type}
    return m


# One race's place markets: standard "To Be Placed" (PLACE, pays 3) + two
# OTHER_PLACE alternates ("2 TBP" pays 2, "4 TBP" pays 4). Same horse in each.
ASCOT_1430_TBP = _place_market("1.500", "PLACE", "To Be Placed", [(101, "Constitution Hill"), (102, "Lunar Glow")])
ASCOT_1430_2TBP = _place_market("1.501", "OTHER_PLACE", "2 TBP", [(111, "Constitution Hill"), (112, "Lunar Glow")])
ASCOT_1430_4TBP = _place_market("1.502", "OTHER_PLACE", "4 TBP", [(121, "Constitution Hill"), (122, "Lunar Glow")])
ASCOT_PLACE_BOOK = {"1.500": 3, "1.501": 2, "1.502": 4}


@pytest.fixture
def place_patches():
    # Main scan returns the standard PLACE market (one per race); the place
    # post-step fetches all variants for the race + their numberOfWinners.
    with patch("backend.services.racing_service.list_racing_markets", return_value=[ASCOT_1430_TBP]), \
         patch("backend.services.racing_service.list_place_markets_for_event",
               return_value=[ASCOT_1430_TBP, ASCOT_1430_2TBP, ASCOT_1430_4TBP]), \
         patch("backend.services.racing_service.get_market_winners", return_value=ASCOT_PLACE_BOOK):
        yield


class TestPlaceCount:
    def test_plain_place_uses_standard_market_and_tags_places(self, place_patches):
        matches = resolve_racing_markets(_bet(market_type="PLACE"), "Constitution Hill to place 20", SESSION)
        assert len(matches) == 1
        assert matches[0]["marketId"] == "1.500"   # standard To Be Placed
        assert matches[0]["places"] == 3

    def test_top_4_selects_the_other_place_market(self, place_patches):
        matches = resolve_racing_markets(_bet(market_type="PLACE", places=4), "Constitution Hill top 4 20", SESSION)
        assert matches[0]["marketId"] == "1.502"
        assert matches[0]["places"] == 4

    def test_top_2_selects_the_two_place_market(self, place_patches):
        matches = resolve_racing_markets(_bet(market_type="PLACE", places=2), "Constitution Hill top 2 20", SESSION)
        assert matches[0]["marketId"] == "1.501"
        assert matches[0]["places"] == 2

    def test_unavailable_place_count_is_declined(self, place_patches):
        with pytest.raises(RacingClarificationError):
            resolve_racing_markets(_bet(market_type="PLACE", places=5), "Constitution Hill top 5 20", SESSION)

    def test_top_4_falls_back_to_market_name_when_book_lacks_winners(self):
        # numberOfWinners absent from the book (empty/None) → derive the places
        # paid from the OTHER_PLACE market name ('4 TBP' → 4) rather than wrongly
        # declining a market that genuinely exists.
        with patch("backend.services.racing_service.list_racing_markets", return_value=[ASCOT_1430_TBP]), \
             patch("backend.services.racing_service.list_place_markets_for_event",
                   return_value=[ASCOT_1430_TBP, ASCOT_1430_2TBP, ASCOT_1430_4TBP]), \
             patch("backend.services.racing_service.get_market_winners", return_value={}):
            matches = resolve_racing_markets(_bet(market_type="PLACE", places=4), "Constitution Hill top 4 20", SESSION)
        assert matches[0]["marketId"] == "1.502"
        assert matches[0]["places"] == 4


def _bet_comp(selection, competition, places=None):
    b = _bet(selection=selection, market_type="WIN")
    b.competition = competition
    return b


GOLD_CUP_AP = _market("5.111", "Cheltenham Gold Cup", "Cheltenham Gold Cup", [
    (5101, "Galopin Des Champs"), (5102, "Fastorslow"),
], event_id="AP1")
DERBY_AP = _market("5.222", "The Derby", "Epsom Derby", [
    (5201, "City Of Troy"), (5202, "Ancient Wisdom"),
], event_id="AP2")
ROYAL_ASCOT_AP = _market("5.333", "Royal Ascot", "Royal Ascot - Gold Cup", [
    (5301, "Opera Ballo"), (5302, "Kyprios"),
], event_id="AP3")


class TestAntepostFallback:
    def test_win_falls_back_to_antepost_when_no_win_market(self):
        # WIN scan finds nothing; competition named → search ante-post.
        with patch("backend.services.racing_service.list_racing_markets",
                   side_effect=[[ASCOT_1430], [GOLD_CUP_AP, DERBY_AP]]) as fetch:
            matches = resolve_racing_markets(
                _bet_comp("Galopin Des Champs", "Gold Cup"),
                "back Galopin Des Champs to win the Gold Cup 50", SESSION,
            )
        assert len(matches) == 1
        assert matches[0]["marketId"] == "5.111"
        assert matches[0]["marketType"] == "ANTEPOST_WIN"
        # First fetch WIN, second ANTEPOST_WIN.
        assert fetch.call_args_list[0][0][1] == "WIN"
        assert fetch.call_args_list[1][0][1] == "ANTEPOST_WIN"

    def test_win_market_takes_priority_over_antepost(self):
        # Horse has a WIN market today → resolved there, no ante-post fetch.
        with patch("backend.services.racing_service.list_racing_markets",
                   return_value=[ASCOT_1430]) as fetch:
            matches = resolve_racing_markets(
                _bet_comp("Constitution Hill", "Gold Cup"),
                "back Constitution Hill to win the Gold Cup 50", SESSION,
            )
        assert matches[0]["marketType"] == "WIN"
        assert fetch.call_count == 1   # never reached the ante-post fetch

    def test_no_competition_means_no_antepost_fallback(self):
        # 0 WIN matches and no competition → ask for the meeting, don't scan ante-post.
        with patch("backend.services.racing_service.list_racing_markets",
                   return_value=[ASCOT_1430]) as fetch:
            with pytest.raises(RacingClarificationError):
                resolve_racing_markets(_bet(selection="Unknown Horse"), "back Unknown Horse 50", SESSION)
        assert fetch.call_count == 1

    def test_win_falls_back_to_antepost_via_event_name(self):
        # "Royal Ascot" is a festival the parser put in event_name (looks like a
        # venue); competition is None. It must still trigger the ante-post search.
        with patch("backend.services.racing_service.list_racing_markets",
                   side_effect=[[ASCOT_1430], [ROYAL_ASCOT_AP]]):
            matches = resolve_racing_markets(
                _bet(selection="Opera Ballo", market_type="WIN", event_name="Royal Ascot"),
                "back Opera Ballo at Royal Ascot 1", SESSION,
            )
        assert matches[0]["marketId"] == "5.333"
        assert matches[0]["marketType"] == "ANTEPOST_WIN"

    def test_antepost_competition_scopes_the_search(self):
        # "Galopin Des Champs" only in the Gold Cup market; Derby market ignored.
        with patch("backend.services.racing_service.list_racing_markets",
                   side_effect=[[], [GOLD_CUP_AP, DERBY_AP]]):
            matches = resolve_racing_markets(
                _bet_comp("Galopin Des Champs", "Gold Cup"),
                "Galopin Des Champs to win the Gold Cup 50", SESSION,
            )
        assert matches[0]["marketId"] == "5.111"

    def test_antepost_typo_resolved_by_ai(self):
        # Misspelled horse: exact match fails, AI fuzzy-matches over the scoped
        # ante-post pool — same typo tolerance as the WIN Tier-2 path.
        with patch("backend.services.racing_service.list_racing_markets",
                   side_effect=[[ASCOT_1430], [GOLD_CUP_AP, DERBY_AP]]), \
             patch("backend.services.ai_interpreter.AIInterpreter.select_racing_runner",
                   return_value={"market_id": "5.111", "selection_id": 5101}) as mock_pick:
            matches = resolve_racing_markets(
                _bet_comp("Galopin De Champs", "Gold Cup"),
                "back Galopin De Champs to win the Gold Cup 50", SESSION,
            )
        assert matches[0]["marketId"] == "5.111"
        assert matches[0]["selectionId"] == 5101
        assert matches[0]["marketType"] == "ANTEPOST_WIN"
        # The AI only sees the Gold Cup-scoped pool, not the Derby market.
        assert {m["marketId"] for m in mock_pick.call_args[0][1]} == {"5.111"}

    def test_antepost_ai_miss_asks_to_check(self):
        with patch("backend.services.racing_service.list_racing_markets",
                   side_effect=[[ASCOT_1430], [GOLD_CUP_AP]]), \
             patch("backend.services.ai_interpreter.AIInterpreter.select_racing_runner", return_value=None):
            with pytest.raises(RacingClarificationError):
                resolve_racing_markets(
                    _bet_comp("Total Nonsense", "Gold Cup"),
                    "back Total Nonsense to win the Gold Cup 50", SESSION,
                )

    def test_antepost_exact_match_beats_meeting_win_cards(self):
        # The festival already has WIN markets for its OTHER races (so the
        # meeting filter is non-empty), but the target horse is only in
        # ante-post. The exact ante-post match must win — the AI must never be
        # asked to guess over the wrong WIN races (the Senorita Bonita bug).
        royal_ascot_win = _market(
            "6.111", "Royal Ascot 17th Jun", "14:30 Queen Anne",
            [(601, "Decoy One"), (602, "Decoy Two")], event_id="RAW",
        )
        royal_ascot_ap = _market(
            "6.222", "Royal Ascot 17th Jun", "Queen Mary Stakes",
            [(621, "Senorita Bonita"), (622, "Other Filly")], event_id="RAAP",
        )
        with patch("backend.services.racing_service.list_racing_markets",
                   side_effect=[[royal_ascot_win], [royal_ascot_ap]]), \
             patch("backend.services.ai_interpreter.AIInterpreter.select_racing_runner") as mock_pick:
            matches = resolve_racing_markets(
                _bet(selection="Senorita Bonita", event_name="Royal Ascot"),
                "Senorita Bonita to win at Royal Ascot 1", SESSION,
            )
        assert len(matches) == 1
        assert matches[0]["selectionId"] == 621
        assert matches[0]["marketType"] == "ANTEPOST_WIN"
        assert not mock_pick.called   # resolved by exact match, no AI guess

    def test_antepost_typo_with_meeting_win_cards_searches_combined_pool(self):
        # Same setup but the name is MISSPELLED, so exact match fails on both
        # pools. The single AI pass must see the meeting's WIN card AND the
        # festival's ante-post markets together, so it can pick the ante-post
        # runner instead of being boxed into the wrong WIN races.
        royal_ascot_win = _market(
            "6.111", "Royal Ascot 17th Jun", "14:30 Queen Anne",
            [(601, "Decoy One"), (602, "Decoy Two")], event_id="RAW",
        )
        royal_ascot_ap = _market(
            "6.222", "Royal Ascot 17th Jun", "Queen Mary Stakes",
            [(621, "Senorita Bonita"), (622, "Other Filly")], event_id="RAAP",
        )
        with patch("backend.services.racing_service.list_racing_markets",
                   side_effect=[[royal_ascot_win], [royal_ascot_ap]]), \
             patch("backend.services.ai_interpreter.AIInterpreter.select_racing_runner",
                   return_value={"market_id": "6.222", "selection_id": 621}) as mock_pick:
            matches = resolve_racing_markets(
                _bet(selection="Senrita Bonta", event_name="Royal Ascot"),
                "Senrita Bonta to win at Royal Ascot 1", SESSION,
            )
        assert len(matches) == 1
        assert matches[0]["selectionId"] == 621
        assert matches[0]["marketType"] == "ANTEPOST_WIN"
        # One AI call, over BOTH the WIN card and the ante-post market.
        assert mock_pick.call_count == 1
        assert {m["marketId"] for m in mock_pick.call_args[0][1]} == {"6.111", "6.222"}

    def test_antepost_fuzzy_duplicate_name_returns_a_slip_per_race(self):
        # 'Aperoll' is entered in TWO ante-post races. A misspelling ('Aperrol')
        # fails exact matching, so the AI runs — and may cross-wire its pick
        # (market_id from race A, selection_id from race B). Resolution must key
        # off the runner NAME, returning a slip per race rather than 422-ing.
        windsor = _market("7.111", "Royal Ascot 17th Jun", "Windsor Castle Stakes",
                          [(701, "Aperoll"), (702, "Other A")], event_id="W")
        chesham = _market("7.222", "Royal Ascot 17th Jun", "Chesham Stakes",
                          [(711, "Aperoll"), (712, "Other B")], event_id="C")
        with patch("backend.services.racing_service.list_racing_markets",
                   side_effect=[[], [windsor, chesham]]), \
             patch("backend.services.ai_interpreter.AIInterpreter.select_racing_runner",
                   return_value={"market_id": "7.111", "selection_id": 711}):  # cross-wired
            matches = resolve_racing_markets(
                _bet(selection="Aperrol", event_name="Royal Ascot"),
                "Aperrol to win at Royal Ascot 1", SESSION,
            )
        assert {m["marketId"] for m in matches} == {"7.111", "7.222"}
        assert all(m["marketType"] == "ANTEPOST_WIN" for m in matches)
        assert all(m["runnerName"] == "Aperoll" for m in matches)


class TestEachWay:
    def test_each_way_resolves_off_the_native_market(self):
        with patch("backend.services.racing_service.list_racing_markets",
                   return_value=[ASCOT_1430_EW]) as fetch:
            matches = resolve_racing_markets(_bet(market_type="EACH_WAY"), "Constitution Hill each way 10", SESSION)
        # One single-market fetch, of the EACH_WAY type — no PLACE composition.
        assert fetch.call_count == 1
        assert fetch.call_args[0][1] == "EACH_WAY"
        assert len(matches) == 1
        assert matches[0]["marketId"] == "3.111"
        assert matches[0]["selectionId"] == 3101
        assert matches[0]["marketType"] == "EACH_WAY"
