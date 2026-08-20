"""Live verification harness — NOT part of the app.

Logs into Betfair, then drives the real `prepare_bet` route with natural-language
inputs to confirm a real, placeable slip is produced for each market-type group
in Market_Types.md. DOES NOT place any bets (never calls /api/confirm).

Usage (from the repo root):
    python -m scripts.harness_market_types discover <sport>   # dump live events/markets
    python -m scripts.harness_market_types test               # run the input matrix
"""
import os
import sys

from dotenv import load_dotenv
from fastapi import HTTPException

from backend.api import routes
from backend.config.sport_mapping import SPORT_EVENT_TYPE_MAP
from backend.schemas.bets import BetRequest
from backend.services.betfair_auth import login
from backend.services.betfair_client import betfair_post

load_dotenv()

SESSION = {}


class FakeRequest:
    def __init__(self, session):
        self.session = session


def do_login():
    username = os.getenv("BETFAIR_USERNAME")
    password = os.getenv("BETFAIR_PASSWORD")
    login(username, password, SESSION)
    print("Logged in.\n")


def run_prepare(user_input: str):
    """Call the real route. Returns ('ok', slips) | ('clarify', detail) | ('error', detail)."""
    req = FakeRequest(SESSION)
    try:
        slips = routes.prepare_bet(req, BetRequest(user_input=user_input))
        return ("ok", slips)
    except HTTPException as e:
        if isinstance(e.detail, dict) and e.detail.get("status") == "clarification_needed":
            return ("clarify", e.detail.get("clarification_question"))
        return ("error", e.detail)


def discover(sport: str):
    etid = SPORT_EVENT_TYPE_MAP[sport.lower()]
    events = betfair_post("listEvents/", {"filter": {"eventTypeIds": [etid]}}, SESSION)
    events.sort(key=lambda e: e.get("event", {}).get("openDate", ""))
    print(f"=== {sport}: {len(events)} events ===")
    for ev in events[:40]:
        e = ev["event"]
        print(f"  {e.get('name','?'):<45} {e.get('openDate','?')[:16]}  id={e['id']}")


def show_markets(event_id: str, market_types=None):
    """Dump market catalogue (runners) for one event, optionally filtered by type."""
    filt = {"eventIds": [event_id]}
    if market_types:
        filt["marketTypeCodes"] = market_types
    cat = betfair_post(
        "listMarketCatalogue/",
        {"filter": filt, "maxResults": "100",
         "marketProjection": ["RUNNER_DESCRIPTION", "EVENT", "MARKET_DESCRIPTION"]},
        SESSION,
    )
    for m in cat:
        mt = m.get("description", {}).get("marketType", "?")
        print(f"\n  market={m.get('marketName')}  type={mt}  id={m['marketId']}")
        for r in m.get("runners", [])[:12]:
            print(f"      {r.get('runnerName','?'):<30} sel={r.get('selectionId')}  hcap={r.get('handicap')}")


# ── Test matrix ───────────────────────────────────────────────────────────────
# One representative per market-type GROUP (identical-mechanism markets that only
# differ by line/number are tested once). (label, input, expected_market_type).
# expected_market_type is what the resolved slip SHOULD carry; None = don't check.
MATRIX = [
    # ---- H2H name-match (MATCH_ODDS family across sports) ----
    ("football MATCH_ODDS",        "back Fortaleza to beat America Mineiro 5",               "MATCH_ODDS"),
    ("baseball MATCH_ODDS",        "back the New York Yankees to beat the White Sox 5",      "MATCH_ODDS"),
    ("basketball MATCH_ODDS",      "back Indiana Fever to beat Toronto Tempo 5",             "MATCH_ODDS"),
    ("rugbyU MATCH_ODDS",          "back Leinster to beat Bulls 5",                          "MATCH_ODDS"),
    ("rugbyL MATCH_ODDS",          "back Leeds Rhinos to beat Warrington 5",                 "MATCH_ODDS"),
    ("AFL MATCH_ODDS",             "back Geelong to beat Fremantle 5",                       "MATCH_ODDS"),
    ("boxing MATCH_ODDS",          "back Anthony Joshua to beat Kristian Prenga 5",          "MATCH_ODDS"),
    ("mma MATCH_ODDS",             "back Conor McGregor to beat Max Holloway 5",             "MATCH_ODDS"),
    ("darts MATCH_ODDS",           "back Graham Hall to beat Ryan Branley in the darts 5",   "MATCH_ODDS"),
    ("cricket MATCH_ODDS",         "back England to beat New Zealand in the cricket 5",      "MATCH_ODDS"),
    # ---- Over/Under encoded in market type (football) ----
    ("football OVER_UNDER",        "over 2.5 goals in Fortaleza vs America Mineiro 5",       "OVER_UNDER_25"),
    # ---- Yes/No runner ----
    ("football BTTS",              "both teams to score in Fortaleza v America Mineiro 5",   "BOTH_TEAMS_TO_SCORE"),
    # ---- Line markets needing line filter ----
    ("football ASIAN_HANDICAP",    "Fortaleza -0.5 on the asian handicap v America Mineiro 5", "ASIAN_HANDICAP"),
    ("basketball COMBINED_TOTAL",  "under 120.5 total points in Indiana Fever vs Toronto Tempo 5", "COMBINED_TOTAL"),
    ("basketball HANDICAP",        "Indiana Fever -4.5 on the handicap vs Toronto Tempo 5",  "HANDICAP"),
    # ---- Special-naming / AI-runner-fallback ----
    ("football CORRECT_SCORE",     "correct score 2-1 to Fortaleza v America Mineiro 5",     "CORRECT_SCORE"),
    ("football DRAW_NO_BET",       "Fortaleza draw no bet against America Mineiro 5",        "DRAW_NO_BET"),
    ("football DOUBLE_CHANCE",     "Fortaleza double chance against America Mineiro 5",      "DOUBLE_CHANCE"),
    ("football HALF_TIME",         "back Fortaleza to win the first half against America Mineiro 5", "HALF_TIME"),
    ("football HT_FT",             "Fortaleza to lead at half time and win v America Mineiro 5", "HALF_TIME_FULL_TIME"),
    ("rugbyU WINNING_MARGIN",      "Hurricanes to win by 15+ against Chiefs 5",              "WINNING_MARGIN"),
    ("rugbyU COMBINED_TOTAL",      "under 40.5 total points in Leinster v Bulls 5",          "COMBINED_TOTAL"),
    ("rugbyU HANDICAP",            "Leinster -7.5 on the handicap against Bulls 5",          "HANDICAP"),
    ("rugbyU HEAD_TO_HEAD",        "Hurricanes head to head against Chiefs 5",               "UNUSED"),
    ("rugbyL FIRST_TRY_SCORER",    "James Tedesco to score the first try in New South Wales v Queensland 5", "FIRST_TRY_SCORER"),
    ("AFL WINNING_MARGIN",         "Geelong to win by more than 24.5 points against Fremantle 5", "WINNING_MARGIN"),
    ("boxing METHOD_OF_VICTORY",   "Tyson Fury to win by knockout against Joshua 5",         "METHOD_OF_VICTORY"),
    ("boxing ROUND_BETTING",       "Ben Whittaker to win in round 3 against Richard Rivera 5", "ROUND_BETTING"),
    ("boxing GO_THE_DISTANCE",     "Fury v Joshua to go the distance 2",                     "GO_THE_DISTANCE"),
    ("cricket TO_WIN_THE_TOSS",    "England to win the toss against New Zealand 5",          None),
    # ---- Competition outrights ----
    ("golf WINNER",                "back Rory McIlroy to win the US Open 5",                 None),
    ("golf TOP_5_FINISH",          "Rory McIlroy top 5 at the US Open 5",                    None),
    ("golf EACH_WAY",              "Rory McIlroy each way in the US Open 5",                 "EACH_WAY"),
    ("golf MAKE_THE_CUT",          "Rory McIlroy to make the cut at the US Open 5",          None),
    ("motorsport WINNER",          "back Max Verstappen to win the Austrian Grand Prix 5",   None),
    ("motorsport CHAMPIONSHIP",    "back Max Verstappen to win the F1 championship 5",       None),
    ("tennis TOURNAMENT_WINNER",   "back Sabalenka to win WTA Berlin 5",                     None),
    ("rugbyU OUTRIGHT_WINNER",     "back Leinster to win the United Rugby Championship 5",   "OUTRIGHT_WINNER"),
    ("AFL OUTRIGHT",               "back Geelong to win the AFL premiership 5",              None),
    ("politics NONSPORT",          "back Labour to win the next UK general election 5",      None),
    # ---- Racing single-runner ----
    ("horse WIN",                  "back Huey Duey to win 5",                                "WIN"),
    ("horse PLACE",                "Loopy Farooki to be placed at Cambridge 5",              "PLACE"),
    ("horse EACH_WAY",             "Definite Dream each way at Worcester 5",                 "EACH_WAY"),
    ("horse ANTEPOST_WIN",         "ante post on Charles Darwin for the Commonwealth Cup 5", "ANTEPOST_WIN"),
    ("greyhound WIN",              "back the greyhound Crystal Crown at Murray Bridge 5",    "WIN"),
]


def run_matrix(filter_sub=None):
    import time
    rows = []
    for label, inp, expected in MATRIX:
        if filter_sub and filter_sub.lower() not in label.lower():
            continue
        t0 = time.time()
        try:
            status, payload = run_prepare(inp)
        except Exception as e:
            status, payload = "exc", f"{type(e).__name__}: {e}"
        ms = int((time.time() - t0) * 1000)
        rows.append((label, inp, expected, status, payload, ms))

        print(f"\n##### {label}  ({ms} ms)")
        print(f"  input: {inp!r}")
        if status == "ok":
            for s in payload:
                ok = "OK" if (expected is None or s.market_type == expected) else f"MISMATCH(exp {expected})"
                line = f" line={s.line}" if s.line is not None else ""
                pl = f" places={s.places}" if s.places is not None else ""
                print(f"  -> [{ok}] {s.market_type} | runner={s.runner_name or s.selection_name} | "
                      f"{s.side} @ {s.price}{line}{pl} | event={s.event_name}")
        elif status == "clarify":
            print(f"  -> CLARIFY: {payload}")
        else:
            print(f"  -> {status.upper()}: {payload}")

    print("\n\n================ SUMMARY ================")
    for label, _inp, expected, status, payload, ms in rows:
        if status == "ok":
            mt = payload[0].market_type if payload else "?"
            verdict = "SLIP " + ("OK " if (expected is None or mt == expected) else f"!!MT={mt} exp {expected}")
        elif status == "clarify":
            verdict = "CLARIFY"
        else:
            verdict = status.upper()
        print(f"  {verdict:<10} {label:<28} ({ms}ms)")


def racing(market_type="WIN", sport="horse racing", n=4):
    """Show the next few upcoming real races of a type and their runners."""
    from backend.config.sport_mapping import event_type_id_for
    from backend.services.betfair_client import list_racing_markets
    mk = list_racing_markets(event_type_id_for(sport), market_type, SESSION)
    print(f"{len(mk)} {market_type} markets for {sport}")
    for m in mk[:n]:
        ev = m.get("event", {}).get("name", "")
        print(f"\n  {m.get('marketStartTime','')[:16]}  {ev}  | {m.get('marketName')} | id={m['marketId']}")
        for r in m.get("runners", [])[:8]:
            print(f"      {r.get('runnerName','?'):<28} sel={r.get('selectionId')}")


if __name__ == "__main__":
    do_login()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "discover"
    if cmd == "racing":
        racing(*(sys.argv[2:] or ["WIN"]))
    elif cmd == "test":
        run_matrix(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "discover":
        discover(sys.argv[2] if len(sys.argv) > 2 else "horse racing")
    elif cmd == "markets":
        show_markets(sys.argv[2], sys.argv[3:] or None)
    elif cmd == "events_all":
        for s in ["football", "tennis", "cricket", "golf", "horse racing",
                  "greyhound racing", "basketball", "motor sport", "boxing",
                  "rugby union", "rugby league", "australian rules", "baseball",
                  "mixed martial arts", "darts", "snooker", "politics"]:
            try:
                discover(s)
            except Exception as e:
                print(f"  {s}: ERROR {e}")
            print()
