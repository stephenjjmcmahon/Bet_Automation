# Market-type slip-creation verification

Verified on **2026-06-16** against the **live Betfair Exchange** that a natural,
human-like bet produces a **real, placeable slip** (correct event, market, runner,
side, with a live price and enough liquidity to cover the stake) for every
market-type **group** in [Market_Types.md](Market_Types.md). **No bets were placed.**

Harness: [harness_market_types.py](harness_market_types.py) — logs in with the `.env`
credentials and drives the real `POST /api/prepare` route (`backend/api/routes.py`)
with each input, then inspects the returned `PreparedSlip`. Run with
`python harness_market_types.py test`.

## Grouping rationale

Market_Types.md lists ~250 codes, but slip creation is governed by a small number of
**resolution mechanisms**. Per the brief ("test one market from a group of similar
ones"), one representative is tested per mechanism; markets that differ only by line
number, period, or team-side are covered by their group representative.

| # | Mechanism (group) | Representative(s) verified placeable | Covers (examples) |
|---|---|---|---|
| A | Simple name match (team/player/Yes-No/Draw) | football/baseball/basketball/rugbyU/rugbyL/AFL/boxing/mma/darts/cricket **MATCH_ODDS**; **DRAW_NO_BET**; **DOUBLE_CHANCE**; **HALF_TIME**; cricket **TO_WIN_THE_TOSS**; rugby **HEAD_TO_HEAD (UNUSED)** | TIED_MATCH, COMPLETED_MATCH, SET_WINNER, MAP_WINNER, NUMBER_OF_SETS, win-to-nil, ODD_OR_EVEN, SENDING_OFF, PENALTY_TAKEN, all 2/3-way result markets |
| B | Over/Under encoded in the market-type code | football **OVER_UNDER_25** | OVER_UNDER_05…85, FIRST_HALF_GOALS_xx, TEAM_x_OVER_UNDER_xx, OVER_UNDER_xx_CARDS/CORNR |
| C | Line markets (line filter in `get_best_price`) | basketball **COMBINED_TOTAL** & **HANDICAP**; football **ASIAN_HANDICAP**; rugbyU **COMBINED_TOTAL** & **HANDICAP** | ALT_TOTAL_GOALS, TOTAL_MATCH_POINTS, cricket `*_OVR_LINE`/`*_RUNS_LINE`, baseball/esports/gaelic HANDICAP & COMBINED_TOTAL |
| D | AI runner-fallback (per-competition runner naming) | football **CORRECT_SCORE** & **HALF_TIME_FULL_TIME**; rugbyU **WINNING_MARGIN**; rugbyL **FIRST_TRY_SCORER** | METHOD_OF_VICTORY, ROUND_BETTING, FIRST_GOAL_SCORER, SET_BETTING, SET_CORRECT_SCORE, HALF_TIME_SCORE, CORRECT_SCORE2 |
| E | Yes/No two-outcome | football **BOTH_TEAMS_TO_SCORE**; boxing **GO_THE_DISTANCE** | TIED_MATCH, SHOWN_A_CARD, MAKE_THE_CUT (yes/no), GO_THE_DISTANCE |
| F | Competition outright (market on a competition-level event) | golf **WINNER / TOP_5_FINISH / EACH_WAY / MAKE_THE_CUT**; motorsport **WINNER** & **CHAMPIONSHIP_WINNER**; rugbyU **OUTRIGHT_WINNER**; AFL **outright**; tennis **TOURNAMENT_WINNER**; politics **NONSPORT** | TOP_N_FINISH, TOP_NATIONALITY, ROUND_LEADER, SERIES_WINNER, SUPER_BOWL_WINNER, CONSTRUCTORS_WINNER, GROUP_x_WINNER, TO_REACH_FINAL, all football World-Cup specials |
| G | Racing single-runner (deterministic runner scan) | horse **WIN / PLACE / EACH_WAY / ANTEPOST_WIN**; greyhound **WIN** | greyhound PLACE; all single-selection racing markets |

## Result: 42 / 45 representative cells produced a real placeable slip

The 3 cells below **resolve correctly** (right event, market and runner — confirmed in
the debug trace) and fail only the live-liquidity check at the moment of testing. Each
belongs to a group already shown placeable above, so the group is satisfied.

| Cell | Resolution | Why no placeable slip right now |
|---|---|---|
| AFL WINNING_MARGIN | Correct: AI fallback maps "win by over 24.5 pts" → `Geelong over 24.5pts` | `resolve_market` only inspects `markets[0]` ("Winning Margin 24.5"), which is thinly traded (< exchange £2 min). The liquid sub-market ("Winning Margin 39.5") isn't reachable by design. Group covered by **rugbyU WINNING_MARGIN** (placeable). |
| boxing METHOD_OF_VICTORY | Resolves to a method runner | Only Fury–Joshua has any method liquidity, and its book carries stale **REMOVED** duplicate runners ("Tyson Fury by KO/TKO or DQ") that out-match the active ones ("Fury TKO/KO/DQ") on name. All other fights are weeks away with zero liquidity. Group covered by **rugbyU WINNING_MARGIN / football HT-FT / CORRECT_SCORE**. |
| boxing ROUND_BETTING | Correct: AI fallback maps "in round 3" → `Whittaker Round 3` | Round-betting markets are untraded on every current boxing event (fights weeks out). Same AI-fallback group as above; boxing also has **GO_THE_DISTANCE** placeable. |

## Code changes made during verification (parser only — no extra AI calls / latency)

All fixes are inside the existing single `gpt-4o` parse call or the existing
`gpt-4o-mini` event-pick call — the per-bet AI-call count is unchanged (2, +1 only on
the runner-fallback path).

- `backend/services/ai_interpreter.py`
  - HALF_TIME vs HALF_TIME_FULL_TIME disambiguated ("to win the first half" =
    `HALF_TIME`, not the HT/FT double).
  - Added parse rules + example for cricket **TO_WIN_THE_TOSS** (parser previously
    failed to extract the selection and asked for clarification).
  - Added **FIRST_TRY_SCORER** and boxing/MMA **METHOD_OF_VICTORY / ROUND_BETTING /
    GO_THE_DISTANCE** phrasings ("by KO", "go the distance", "win in round N").
  - `select_top_events`: instructed never to pick the rare `UNDIFFERENTIATED`
    catch-all when a specific type fits — this is what broke the politics/election
    (`NONSPORT`) bet.

## Limitations surfaced during verification — now fixed

Both were fixed by making `resolve_market` (search_service.py) **book-aware** while
keeping the per-bet cost unchanged (1 `listMarketCatalogue` + 1 `listMarketBook`; the
book is now fetched in the resolver and threaded into `get_best_price` via a new
optional `book=` param instead of being fetched twice). Covered by
`backend/tests/test_resolve_market.py`.

- **Withdrawn/REMOVED runners** stay in the catalogue (which carries no status), so a
  bet could match a dead runner (a non-runner horse, or a re-listed boxing market's
  stale duplicate) and then fail the price check. Now the resolver reads runner status
  from the book and **re-picks among the ACTIVE runners**. Verified live: boxing
  `METHOD_OF_VICTORY` on Fury–Joshua now skips the stale `Tyson Fury by KO/TKO or DQ`
  and lands the live `Fury TKO/KO/DQ` (£99 @ 2.16) → placeable slip.
- **Multiple sub-markets of one type** (e.g. AFL's four `WINNING_MARGIN` markets):
  `resolve_market` used to inspect only `markets[0]`. It now matches across **every**
  market of the type — by runner name (line-preferred) and, on a name miss, via a
  single AI fallback over the union of all those markets' runners — so alternate-line
  markets are reachable. Verified live: an "over 39.5" margin bet now resolves into a
  39.5/spread market instead of being stuck on the 24.5 market.
