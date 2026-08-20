"""Runner-matching corpus.

Each case is one market's runner list plus the selection_name the parser
produced, and the selection id a correct matcher should return.

`source` records how much to trust the case, and is reported in the summary:

  logged        selection_name is taken VERBATIM from logs/interpreter.jsonl —
                a real thing this app was asked to resolve.
  repo-fixture  taken from the existing test suite (already-observed shapes).
  documented    runner naming follows the Betfair conventions recorded in
                CLAUDE.md, which were derived from live observation.
  synthetic     constructed edge case, included to pin behaviour rather than to
                claim frequency.

Runner LISTS are reconstructions in every case — the app has never logged them.
So this corpus measures a matcher against Betfair's documented naming, not
against a captured live sample; treat the pass rate as a regression score and a
way to compare strategies, not as a field accuracy figure. `expected=None` means
"no confident match — fall through to the AI runner fallback", which is a
correct outcome, not a failure.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    id: str
    selection_name: str
    runners: tuple
    expected: int | None
    market_type: str
    source: str
    note: str = ""
    ambiguous: bool = field(default=False)


def _r(*pairs):
    return tuple({"selectionId": sid, "runnerName": name} for sid, name in pairs)


CASES = [
    # ── straightforward name matches (must not regress) ───────────────────────
    Case(
        id="match_odds/simple",
        selection_name="Leinster",
        runners=_r((1, "Leinster"), (2, "Bulls"), (3, "The Draw")),
        expected=1, market_type="MATCH_ODDS", source="logged",
        note="Baseline: exact team name present.",
    ),
    Case(
        id="match_odds/full_name_vs_short_runner",
        selection_name="Northampton Saints",
        runners=_r((1, "Northampton"), (2, "Exeter"), (3, "The Draw")),
        expected=1, market_type="MATCH_ODDS", source="logged",
        note="Reverse containment doing its job — the runner is named more tersely "
             "than the request. Any fix to the reverse arm must keep this.",
    ),
    Case(
        id="match_odds/spurs",
        selection_name="San Antonio Spurs",
        runners=_r((1, "San Antonio Spurs"), (2, "Denver Nuggets")),
        expected=1, market_type="MATCH_ODDS", source="logged",
    ),
    Case(
        id="draw_no_bet/team",
        selection_name="Fortaleza",
        runners=_r((1, "Fortaleza"), (2, "Sao Paulo")),
        expected=1, market_type="DRAW_NO_BET", source="logged",
    ),
    Case(
        id="unused/head_to_head",
        selection_name="Hurricanes",
        runners=_r((1, "Hurricanes"), (2, "Chiefs")),
        expected=1, market_type="UNUSED", source="logged",
    ),

    # ── exact-vs-substring collisions (A2) ────────────────────────────────────
    Case(
        id="scorer/exact_listed_last",
        selection_name="Gabriel",
        runners=_r((1, "Gabriel Jesus"), (2, "Gabriel Martinelli"), (3, "Gabriel")),
        expected=3, market_type="FIRST_GOAL_SCORER", source="documented",
        note="A runner named exactly as asked, listed after two longer near-misses. "
             "Catalogue order decides the answer under the shipped matcher.",
    ),
    Case(
        id="scorer/exact_listed_first",
        selection_name="Gabriel",
        runners=_r((3, "Gabriel"), (1, "Gabriel Jesus"), (2, "Gabriel Martinelli")),
        expected=3, market_type="FIRST_GOAL_SCORER", source="documented",
        note="Same card, exact runner first. Shipped matcher happens to be right "
             "here — the pair shows the answer is order-dependent.",
    ),
    Case(
        id="racing/short_name_prefix_collision",
        selection_name="Shallow",
        runners=_r((1, "Shallow Hal"), (2, "Shallow")),
        expected=2, market_type="WIN", source="logged",
        note="Real logged horse name. Short racing names collide constantly across "
             "a ~450-market scan.",
    ),
    Case(
        id="racing/everest",
        selection_name="Everest",
        runners=_r((1, "Everest Rising"), (2, "Everest")),
        expected=2, market_type="WIN", source="logged",
    ),
    Case(
        id="golf/surname_only",
        selection_name="Scottie",
        runners=_r((1, "Scottie Scheffler"), (2, "Collin Morikawa")),
        expected=1, market_type="OUTRIGHT_WINNER", source="logged",
        note="Partial name with no exact rival — forward containment must still work.",
    ),

    # ── reverse-containment hazards (A3) ──────────────────────────────────────
    Case(
        id="method/two_fighters_named",
        selection_name="Fury v Joshua",
        runners=_r((1, "Joshua"), (2, "Fury")),
        expected=None, market_type="METHOD_OF_VICTORY", source="logged",
        ambiguous=True,
        note="REAL logged input naming BOTH fighters. Shipped matcher silently "
             "returns whichever is listed first — here 'Joshua', i.e. a bet on the "
             "opponent. Correct outcome is no confident match, so the AI fallback "
             "(which sees the full user_input) decides.",
    ),
    Case(
        id="method/long_request_swallows_runner",
        selection_name="Anthony Joshua v Prenga to go the distance",
        runners=_r((1, "Anthony Joshua"), (2, "Prenga"), (3, "Yes"), (4, "No")),
        expected=None, market_type="METHOD_OF_VICTORY", source="logged",
        ambiguous=True,
        note="REAL logged input. Every runner name is a whole word inside the "
             "request, so no single runner is the confident answer.",
    ),
    Case(
        id="match_odds/draw_swallowed",
        selection_name="Arsenal to draw no bet",
        runners=_r((1, "Draw"), (2, "Arsenal"), (3, "Burnley")),
        expected=None, market_type="MATCH_ODDS", source="synthetic",
        ambiguous=True,
        note="Short runner name swallowing a longer request. Constructed — the "
             "parser normally emits a clean selection_name, so treat as a guard "
             "rather than evidence of frequency.",
    ),

    # ── line / total markets ──────────────────────────────────────────────────
    Case(
        id="over_under/goals_suffix",
        selection_name="Over 2.5",
        runners=_r((1, "Over 2.5 Goals"), (2, "Under 2.5 Goals")),
        expected=1, market_type="OVER_UNDER_25", source="logged",
    ),
    Case(
        id="combined_total/bare_over",
        selection_name="Under",
        runners=_r((1, "Over"), (2, "Under")),
        expected=2, market_type="COMBINED_TOTAL", source="logged",
        note="COMBINED_TOTAL runners are bare Over/Under; the line picks the row "
             "later in get_best_price.",
    ),

    # ── per-competition naming that should fall through to the AI ─────────────
    Case(
        id="winning_margin/spacing_variant",
        selection_name="Hurricanes 15+",
        runners=_r((1, "Hurricanes 1-14"), (2, "Hurricanes 15 +"), (3, "Chiefs 15 +")),
        expected=None, market_type="WINNING_MARGIN", source="logged",
        note="'15+' vs '15 +' — documented as an AI-fallback case. Matching "
             "nothing here is correct.",
    ),
    Case(
        id="htft/pair",
        selection_name="Northampton - Northampton",
        runners=_r((1, "Northampton - Northampton"), (2, "Northampton - Exeter"),
                   (3, "Any Draw")),
        expected=1, market_type="HALF_TIME_FULL_TIME", source="logged",
    ),
    Case(
        id="method/by_ko_stale_duplicate",
        selection_name="Tyson Fury by KO",
        runners=_r((10, "Tyson Fury by KO/TKO or DQ"), (11, "Fury TKO/KO/DQ")),
        expected=10, market_type="METHOD_OF_VICTORY", source="repo-fixture",
        note="From test_resolve_market.py. Forward containment picks the catalogue "
             "entry; the book's ACTIVE check re-picks if it is stale.",
    ),

    # ── typos: no matcher should match; the AI fallback owns these ────────────
    Case(
        id="handicap/typo_team",
        selection_name="Northimpton",
        runners=_r((1, "Northampton"), (2, "Exeter")),
        expected=None, market_type="HANDICAP", source="logged",
        note="Real logged typo. Substring matching cannot and should not catch it.",
    ),
    Case(
        id="racing/typo_horse",
        selection_name="Perses Way",
        runners=_r((1, "Perseus Way"), (2, "Definite Dream")),
        expected=None, market_type="WIN", source="logged",
        note="Real logged typo — both spellings appear in the log.",
    ),

    # ── correct score / literal tokens ────────────────────────────────────────
    Case(
        id="correct_score/2_1",
        selection_name="2-1",
        runners=_r((1, "2 - 1"), (2, "2-1"), (3, "1-2")),
        expected=2, market_type="CORRECT_SCORE", source="logged",
        note="Exact literal exists alongside a spaced variant.",
    ),
]


def by_source() -> dict:
    counts: dict = {}
    for case in CASES:
        counts[case.source] = counts.get(case.source, 0) + 1
    return counts
