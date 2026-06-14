# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **For Claude:** This file is intended to be continuously improved. When you learn something non-obvious about the codebase, a quirk of the Betfair API, or a preference from the user, add it here so the next conversation benefits.

---

## What this project is

A Betfair Exchange betting app with a natural language interface. The user types a plain-English bet ("back Man City to win £20"), the app parses it with GPT-4o, finds the matching event and market on Betfair, fetches a live price, and presents a confirmation slip. The user confirms and the bet is placed via the Betfair API.

The frontend is a single HTML file (`bet_automation.html`) served by the FastAPI backend at `/`.

---

## Running the app

```powershell
# Activate venv (Windows)
venv\Scripts\activate

# Run backend (serves frontend at http://localhost:8000)
uvicorn backend.main:app --reload
```

## Tests

```powershell
pytest                              # all tests
pytest backend/tests/test_odds_service.py   # single file
pytest -k "test_confirmation"       # single test by name
```

---

## Required `.env` file

Create `.env` in the project root with:

```
BETFAIR_APP_KEY=...
BETFAIR_USERNAME=...
BETFAIR_PASSWORD=...
OPENAI_API_KEY=...
SESSION_SECRET_KEY=...   # optional, defaults to insecure dev value
```

---

## Architecture: how a bet flows end-to-end

`POST /api/prepare` is the core endpoint. Here's the full flow:

1. **Parse** — `AIInterpreter.interpret(user_input)` calls GPT-4o with a detailed system prompt to produce a `ParsedBet` (selection, sport, side, stake, market type, line, opponent, competition). If required fields are missing it returns `clarification_needed` and the frontend asks a follow-up question before retrying.

2. **Find events** — Two paths depending on sport:
   - **H2H sports** (football, tennis, basketball, etc.): `find_event_candidates()` — text query search on Betfair using `event_name` (or falls back to `selection_name`).
   - **Competition sports** (`COMPETITION_SPORTS` set — golf, horse racing, motor sport, greyhound racing, cycling, politics): `find_all_events_for_sport()` — fetches *all* events for the sport because there's no meaningful H2H name to search by.

3. **Get market types** — `get_market_types()` fetches available market type strings in one API call (batched event IDs for H2H, sport-level for competition sports).

4. **AI event + market selection** — `AIInterpreter.select_top_events()` calls GPT-4o-mini with the candidate events and available market types, returning up to 3 `{event_id, market_type}` pairs ranked best to worst.

5. **Resolve market** — `resolve_market()` calls `listMarketCatalogue` to get the actual `marketId` and `selectionId`. Runner name matching is fuzzy (substring match in either direction) via `market_resolver.resolve_selection()`.

6. **LINE market special case** — Markets with `_LINE` suffix (e.g. `COMBINED_TOTAL` for basketball totals) don't name-match runners. They always take the first runner and match by `handicap` value. Side is also inverted: "Under" → BACK, "Over" → LAY (Betfair's line market structure).

7. **Live price + liquidity check** — `get_best_price()` fetches the market book and validates that available size at the best price covers the stake. Raises `MarketSuspendedError` or `InsufficientLiquidityError` if not viable.

8. **Pending slip** — A `PreparedSlip` is stored in the user's Starlette session (`pending_slips` dict keyed by UUID). The frontend shows the slip for user confirmation.

9. **Confirm** — `POST /api/confirm/{slip_id}` pops the slip from the session and calls `place_orders()` on Betfair.

---

## Session management

Betfair tokens are stored per-user in a **Starlette signed cookie session** (not server-side). `_require_session` is a FastAPI dependency that calls `get_token(request.session)` — it raises `SessionExpiredError` if the token is absent, which the global exception handler converts to a 401, which the frontend intercepts to re-show the login screen.

The Betfair login endpoint is `POST /api/login` — it calls `betfair_auth.login()` which hits Betfair's interactive SSO with form-encoded body (not JSON). HTTP 200 doesn't mean success — Betfair signals failures in the response body `status` field.

---

## Logging

All logs go in `logs/` (auto-created):

| File | Contents |
|---|---|
| `logs/betting.db` | SQLite — `bets` table (one row per slip, status updated through lifecycle: `prepared → confirmed / expired`) + `failures` table (requests that never produced a slip) |
| `logs/interpreter.jsonl` | Raw GPT-4o input/output for every parse call |
| `logs/feedback.jsonl` | User feedback submitted via the frontend |

---

## Sport / market type configuration

`backend/config/sport_mapping.py` contains two dicts:
- `SPORT_EVENT_TYPE_MAP` — maps sport name strings to Betfair event type IDs
- `COMPETITION_SPORTS` — set of sports that use the all-events fetch path (see step 2 above)

`explore_betfair.py` is a standalone script (not part of the app) for exploring what markets and events are available on Betfair for debugging and discovery. Run it directly: `python explore_betfair.py`.

---

## Two AI models in use

| Model | Where | Purpose |
|---|---|---|
| `gpt-4o` | `AIInterpreter.interpret()` | Structured parsing of natural language → `ParsedBet`. Uses `response_format=BetOutput` for guaranteed JSON schema output. |
| `gpt-4o-mini` | `AIInterpreter.select_top_events()` | Ranking candidate Betfair events + picking the right market type. Cheaper because it's a simpler matching task. |

---

## Racing (horse / greyhound) — different resolution path

Betfair models racing as: **event = meeting** ("Ascot 12th Jun"), **market = individual race** (market name is the off-time), **runner = horse/dog**. `textQuery` never matches runner names, so you can't search Betfair for a horse.

`RACING_SPORTS` (in `sport_mapping.py`) therefore bypass the AI event pick (`select_top_events`) entirely. `resolve_racing_markets()` in `search_service.py`:

1. **Tier 1 (no AI):** one bulk `listMarketCatalogue` (`list_racing_markets()`) fetches every upcoming market of the bet's type (~450 WIN markets on a busy day; `marketStartTime.from = now`, no upper bound so bets days ahead work), then scans all runners for the name. A horse runs at most once per day, so a clean match identifies the race. 1–3 matches → that many slips (user picks, e.g. same greyhound name at two tracks). 0 or >3 → 422 clarification asking for the meeting.
2. **Tier 2:** the clarification retry arrives with `event_name` = track; scope to that meeting's races, and if exact matching still fails, `AIInterpreter.select_racing_runner()` (gpt-4o-mini) does typo-tolerant matching over that one card's runners.

Supported racing market types — all single-runner, all resolved identically (only the fetched market type differs): `WIN` (default — the parser may emit MATCH_ODDS/OUTRIGHT_WINNER, mapped to WIN), `PLACE`, `ANTEPOST_WIN` (explicit keyword "ante post", *or* the WIN→ante-post fallback below), and `EACH_WAY` (Betfair's **native** each-way market — one back settles win + place per the market's `eachWayDivisor`; no composition). FORECAST/REV_FORECAST/MATCH_BET/RACE_WIN_DIST/etc. are multi-selection or non-runner markets that don't fit the single-selection slip model — `RACING_UNSUPPORTED_MESSAGES` declines them cleanly.

Caveat on EACH_WAY: only ~34 EACH_WAY markets exist on a busy day (vs 441 WIN), and they're thinly traded, so a lot of races won't have one — those return the normal `MarketSuspendedError`/`InsufficientLiquidityError`/not-found path.

**WIN → ante-post fallback.** A horse entered only for a future race has no WIN market yet, so a plain "to win the Gold Cup" bet finds nothing in the WIN scan. When a WIN scan returns 0 exact matches and a race/festival is named — in **either** `competition` ("the Gold Cup") **or** `event_name` ("Royal Ascot", which the parser reads as a venue since it contains "Ascot") — `_antepost_pool` builds the `ANTEPOST_WIN` pool scoped to that name (falling back to all ante-post markets if the scope matches none, letting the runner name disambiguate). **Precedence matters and is the subtle bit. Exact always beats fuzzy, and the single fuzzy pass spans both pools.** A festival often has WIN markets for its *other* races (Royal Ascot's WIN cards appear days ahead), so the meeting filter can be non-empty while the target horse lives only in ante-post. Resolution order: (1) exact WIN scan, (2) exact ante-post scan (`_antepost_exact`) — a horse named exactly as typed in a future race wins here even when the meeting also has WIN cards, (3) **one** `select_racing_runner` (gpt-4o-mini) call over the **combined** pool — the meeting's WIN card *plus* the festival's ante-post markets, deduped by market id, each tagged with its own market type — then (4) clarification. The combined fuzzy pass is the fix for a *misspelled* name: a fixed order biases wrong (WIN-pool-first hallucinates a wrong WIN-card horse when the target is in ante-post; ante-post-first does the reverse), so the model sees every real candidate at once and picks the globally closest. **Resolving the AI's pick keys off its `selection_id`, not its `market_id`:** a horse entered in several races (ante-post 2yos commonly hold 2-3 engagements, e.g. "Aperoll" in both the Windsor Castle *and* Chesham Stakes) makes the model cross-wire the pair (market from race A, selection from race B). So we use `selection_id` only to recover the runner *name*, then deterministically exact-re-scan the pool for that name — keeping market+selection consistent and returning one slip per race (capped at `MAX_RACING_MATCHES`), exactly like the exact path's multi-match handling. The chosen market's type (WIN vs ANTEPOST_WIN) comes from whichever pool it was in. WIN always wins ties — a horse with a race today resolves to WIN (found in the unscoped full scan) and the ante-post fetch never happens. No race/festival named → the normal "which meeting?" clarification instead.

**Place markets — top 2/3/4.** A race has one standard `PLACE` market ("To Be Placed", `numberOfWinners` set by field size — usually 2–3) plus `OTHER_PLACE` alternates named "2 TBP"/"4 TBP". They differ only by `numberOfWinners`, which is **only on `listMarketBook`, not the catalogue**. The parser captures a requested count in `ParsedBet.places` ("top 4" → `places=4`; plain "to place" → `places=None`). The main scan stays on `PLACE` (one market per race, so tiering is unchanged); then `_select_place_market` fetches that race's `PLACE`+`OTHER_PLACE` markets (`list_place_markets_for_event`) and their `numberOfWinners` (`get_market_winners`), and picks the market paying `places` — or the standard `PLACE` market when `places` is None (preferring market type `PLACE` on a tie). If the requested count isn't offered, it raises `RacingClarificationError`. The chosen market's places-paid is surfaced on `PreparedSlip.places`.

## Known stale tests

`test_confirmation_gate.py` and `test_event_selection.py` predate the clarification-response refactor (they mock `interpret` returning a bare `ParsedBet`, and `select_top_events` returning plain event-id strings instead of `{event_id, market_type}` dicts) — 24 tests fail on `main` for this reason, unrelated to newer work.

## Betfair API quirks

- `listEvents` `textQuery` matches **competition names as well as event names** — `textQuery="World Cup"` returns every fixture in the FIFA World Cup competition ("Mexico v South Africa" etc.), not just events named "World Cup". For outright bets we don't want those fixtures, so `find_event_candidates()` filters candidates to events whose own name contains the query when `market_type == "OUTRIGHT_WINNER"` (falling back to the unfiltered list if nothing matches).
- Outright/tournament-winner markets hang off a **competition-level event** (e.g. "FIFA World Cup", id distinct from any fixture). The football outright market type code is `WINNER` — not `OUTRIGHT_WINNER` (that's only the parser's internal label). Related codes on the same event: `TO_REACH_FINAL`, `TO_REACH_SEMIS`, `GOLDEN_BOOT`, `GROUP_X_WINNER`, etc.

---

## Reference documentation

- **Betfair Exchange API docs**: https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/overview — official documentation for the Betting API (`listEvents`, `listMarketCatalogue`, `listMarketBook`, `placeOrders`, etc.). Consult this when debugging API behavior rather than guessing at semantics.
