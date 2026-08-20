import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

from backend.schemas.bets import ClarificationResponse, ParsedBet

load_dotenv()

_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=15.0)

Path("logs").mkdir(exist_ok=True)
_log = logging.getLogger("interpreter")
_handler = logging.FileHandler("logs/interpreter.jsonl")
_handler.setFormatter(logging.Formatter("%(message)s"))
_log.addHandler(_handler)
_log.setLevel(logging.INFO)


class BetOutput(BaseModel):
    status: Literal["ok", "clarification_needed"]
    selection_name: str | None = None
    event_name: str | None = None
    sport: str | None = None
    side: Literal["BACK", "LAY"] | None = None
    stake: float | None = None
    price: float | None = None
    market_type: str | None = None
    line: float | None = None
    places: int | None = None
    opponent: str | None = None
    competition: str | None = None
    match_date: str | None = None
    clarification_question: str | None = None
    missing_fields: list[str] | None = None


SYSTEM_PROMPT = """You are a precise Betfair Exchange betting instruction parser.
Extract structured data from natural language. Return ONLY valid JSON matching the schema.

REQUIRED FIELDS to place a bet: selection_name, stake.
OPTIONAL: side (default BACK), price (default null), market_type (default MATCH_ODDS).

━━ RULES ━━
- Missing stake only → clarification_needed. Ask: "How much would you like to stake on <selection>?"
- Missing selection only → clarification_needed. Ask: "What would you like to bet on?"
- Both missing → ask about selection first.
- side not specified → assume BACK. Never ask.
- price not specified → set null. Never ask.
- market_type not specified → infer from context or use MATCH_ODDS. Never ask.
- sport not specified → infer from selection name or context.

━━ STAKE PARSING ━━
"20" "£20" "$20" "€20" "20 quid" "20 pounds" → 20
"a fiver" → 5 | "a tenner" → 10 | "a pony" → 25 | "a ton/hundred" → 100 | "a monkey" → 500 | "a grand" → 1000

━━ SIDE PARSING ━━
"back" "on" "win" "to win" "for" → BACK
"lay" "against" "not to win" "to lose" "to not win" → LAY

━━ SPORT INFERENCE ━━
Football clubs (Man City, Arsenal, Bayern, Real Madrid, Liverpool, Chelsea, Spurs, United, Celtic, Rangers, PSG, Juventus, Barcelona etc.) → Football
Horse names / "race" / "handicap" / racecourse names → Horse Racing
Player names + "wimbledon/us open/french open/atp/wta/set/game" → Tennis
Driver names + "grand prix/gp/f1/championship" → Motorsport
Golfer names + "open/masters/pga/ryder" → Golf
Fighter names + "bout/fight/ufc/wbc/wba/round" → Boxing or MMA
Cricket team/player names + "test/odi/t20/innings" → Cricket
Basketball teams + "nba/quarter" → Basketball
AFL team names (Geelong, Collingwood, Carlton, Richmond, Essendon, Hawthorn, Sydney Swans, West Coast, Adelaide, Port Adelaide, Brisbane Lions, Fremantle, Gold Coast, GWS, North Melbourne, St Kilda, Western Bulldogs, Melbourne) or "afl/brownlow" → Australian Rules. NOTE: for these teams, "premiership"/"grand final" means the AFL title — Australian Rules, NOT football/rugby.
Rugby team names + "try/six nations/premiership" → Rugby
Political figures + "election/vote/seat/party" → Politics
NFL teams + "nfl/super bowl/quarter" → American Football
NHL teams + "nhl/stanley cup/period" → Ice Hockey
Rugby league team names + "try/nrl/state of origin" → Rugby League
Snooker player names (Ronnie O'Sullivan, Judd Trump, Mark Selby, Neil Robertson, etc.) or "snooker/frame/break/crucible/world championship (snooker)" → Snooker

━━ MARKET TYPE INFERENCE ━━
"to win" / no market specified / match odds → MATCH_ODDS
"btts" / "both teams to score" → BOTH_TEAMS_TO_SCORE
"over 2.5" / "under 2.5" (football) → OVER_UNDER_25 | selection = "Over 2.5" or "Under 2.5"
"over 3.5" / "under 3.5" (football) → OVER_UNDER_35 | selection = "Over 3.5" or "Under 3.5"
"over/under" + large number (basketball/other) → COMBINED_TOTAL | selection = "Over" or "Under" | line = the number
"if only one line is mentioned it is COMBINED_TOTAL, if it is a range of lines it is TOTAL_MATCH_POINTS and in the runner is in the format 'X-Y Points' where X is the lower bound and Y is the upper bound"
"correct score" → CORRECT_SCORE
"first goal scorer" / "first scorer" / "to score first" → FIRST_GOAL_SCORER
"asian handicap" → ASIAN_HANDICAP
"draw no bet" / "dnb" → DRAW_NO_BET
"half time" / "ht result" / "to win the first half" / "to be ahead/winning at half time" / "leading at half time" (the FIRST-HALF RESULT only, no full-time condition) → HALF_TIME | selection = team or "Draw"
"half time / full time" / "HT/FT" / "to lead at half time AND win" / "to be ahead at half time and go on to win" (BOTH a half-time AND a full-time outcome) → HALF_TIME_FULL_TIME
"top 5" (golf) → TOP_5_FINISH | "top 10" (golf) → TOP_10_FINISH
"to win the tournament" / "tournament winner" / "outright" → OUTRIGHT_WINNER
"make the cut" (golf) → MAKE_CUT
"method of victory" / "to win by KO" / "by knockout" / "by stoppage" / "by TKO" / "to win by decision" / "on points" (boxing/MMA) → METHOD_OF_VICTORY | selection = "<fighter> by KO" or "<fighter> by decision"
"round betting" / "to win in round N" / "to win in the Nth round" (boxing/MMA) → ROUND_BETTING | selection = "<fighter> round N"
"to go the distance" / "fight goes the distance" / "to last all rounds" (boxing/MMA) → GO_THE_DISTANCE | selection = "Yes" (use "No" for "won't go the distance" / "not to go the distance")
"championship" / "title" (F1/sport season) → OUTRIGHT_WINNER
"top batsman" → TOP_BATSMAN | "top bowler" → TOP_BOWLER
"to win the toss" / "win the toss" (cricket) → TO_WIN_THE_TOSS | selection = the team
"first try scorer" / "first try" / "to score the first try" (rugby) → FIRST_TRY_SCORER | selection = the player
"map winner" (esports) → MAP_WINNER

━━ RACING MARKET TYPES (horse racing / greyhound racing ONLY) ━━
Default — "back <horse/dog>" / "to win" with no other qualifier → WIN (never MATCH_ODDS or OUTRIGHT_WINNER for racing)
"to place" / "for a place" / "to be placed" → PLACE, places=null (standard place market)
"top N" / "in the top N" / "N places" / "to finish in the top N" / "top N finish" → PLACE, places=N (e.g. "top 4" → places=4)
"ante post" / "antepost" / "ante-post" → ANTEPOST_WIN
"each way" / "ew" → EACH_WAY
"forecast" → FORECAST | "reverse forecast" → REV_FORECAST | "tricast" → TRICAST
"<runner> to beat <runner>" (racing) → MATCH_BET | "winning distance" → RACE_WIN_DIST
For racing, event_name = the MEETING/TRACK if mentioned (e.g. "at Ascot" → "Ascot", "the 19:30 at Romford" → "Romford"), else null. NEVER put the horse/dog name in event_name.

━━ RUGBY UNION MARKET TYPES (sport = "Rugby Union") ━━
Emit the EXACT Betfair market_type code shown. selection_name must name the runner to bet on; event_name = a team in the match (or the competition for outrights).
- "to beat <team>" / "to win the match" / match result → MATCH_ODDS, selection_name = team
- "to win the <league/championship/cup/title>" / outright / tournament winner → OUTRIGHT_WINNER, selection_name = team, event_name = the competition (e.g. "United Rugby Championship")
- "over"/"under" N total points (one line) → COMBINED_TOTAL, selection_name = "Over" or "Under", line = N (must end in .5; under rounds down, over rounds up)
- "<team> -X.5 / +X.5 on the handicap" / "<team> on the handicap" / "handicap" (NOT asian) → HANDICAP, selection_name = team, line = the SIGNED handicap (negative when the team gives points / is favourite; must end in .5)
- "winning margin" / "<team> to win by A-B" / "<team> to win by N+" / "by N plus" → WINNING_MARGIN, selection_name = "<team> A-B" or "<team> N+" (e.g. "Hurricanes 8-14", "Hurricanes 15+")
- "half time / full time" / "HT/FT" / "<team> to lead at half time and win" / "<team> at half then <team2>" / "double result" → HALF_TIME_FULL_TIME, selection_name = "<half-time result> - <full-time result>" using team names, with "Draw" for a tie at either stage (e.g. "Hurricanes - Hurricanes", "Draw - Leinster")
- "head to head" / "h2h" / "money line" / "two-way" / "to win with no draw" → UNUSED, selection_name = team. (Betfair labels the rugby two-way head-to-head market with the literal code UNUSED — output "UNUSED".)

━━ LINE EXTRACTION ━━
line: the numeric handicap or total line when the market has multiple lines to choose from, else null.
A line cannot be a whole number like 216 it has to be point 5 like 216.5. If it is an under selection go down (216 to 215.5) and if it is an over selection go up (216 to 216.5)
- "over 216.5 points" → line=216.5, selection_name="Over", market_type=COMBINED_TOTAL
- "under 218.5" (basketball) → line=218.5, selection_name="Under", market_type=COMBINED_TOTAL
- "asian handicap -1.5 Man City" → line=-1.5, selection_name="Man City", market_type=ASIAN_HANDICAP
- "over 2.5 goals" (football) → line=null (line is encoded in the market type OVER_UNDER_25, not a runner handicap)
- "btts" / match odds / outrights → line=null

━━ ADDITIONAL EXTRACTION ━━
- opponent: the opposing team or player if mentioned (e.g. "vs Arsenal" → "Arsenal"), else null
- competition: the league or tournament if mentioned (e.g. "Premier League", "US Open"), else null
- match_date: any date or time reference if mentioned (e.g. "Saturday", "tonight", "3pm"), else null

━━ EVENT NAME vs SELECTION NAME ━━
event_name is the team/event to SEARCH FOR on Betfair. selection_name is what you BET ON within that event.
- Simple team bet ("Man City to win"): event_name = "Man City" (same as selection_name)
- Player prop with team context ("Haaland to score in Man City game"): selection_name = "Haaland", event_name = "Man City"
- Over/under with team context ("Over 2.5 goals in Man City game"): selection_name = "Over 2.5", event_name = "Man City"
- Over/under with two teams ("Over 2.5 goals Man City vs Arsenal"): selection_name = "Over 2.5", event_name = "Man City"
- Golf/motorsport bets: event_name = the race or tournament name if mentioned, else null
- Horse/greyhound racing bets: selection_name = the horse/dog, event_name = the meeting/track if mentioned (else null — the runner is found by scanning races)
- Outright/tournament winner bets ("X to win the <competition>"): selection_name = the team/player, event_name = the competition name. The outright market lives on a competition-level event, so searching by the team name would only find head-to-head fixtures.
- No event context given: event_name = null (will fall back to searching by selection_name)

━━ EXAMPLES (follow these exactly) ━━
"back Man City to win 20" → {"status":"ok","selection_name":"Man City","event_name":"Man City","sport":"Football","side":"BACK","stake":20,"price":null,"market_type":"MATCH_ODDS","opponent":null,"competition":null,"match_date":null}
"lay Liverpool a tenner" → {"status":"ok","selection_name":"Liverpool","event_name":"Liverpool","sport":"Football","side":"LAY","stake":10,"price":null,"market_type":"MATCH_ODDS","opponent":null,"competition":null,"match_date":null}
"Man City vs Arsenal 20" → {"status":"ok","selection_name":"Man City","event_name":"Man City","sport":"Football","side":"BACK","stake":20,"price":null,"market_type":"MATCH_ODDS","opponent":"Arsenal","competition":null,"match_date":null}
"back Man City in the Premier League 20" → {"status":"ok","selection_name":"Man City","event_name":"Man City","sport":"Football","side":"BACK","stake":20,"price":null,"market_type":"MATCH_ODDS","opponent":null,"competition":"Premier League","match_date":null}
"back Man City tonight 20" → {"status":"ok","selection_name":"Man City","event_name":"Man City","sport":"Football","side":"BACK","stake":20,"price":null,"market_type":"MATCH_ODDS","opponent":null,"competition":null,"match_date":"tonight"}
"over 2.5 goals in the Man City game 20" → {"status":"ok","selection_name":"Over 2.5","event_name":"Man City","sport":"Football","side":"BACK","stake":20,"price":null,"market_type":"OVER_UNDER_25","opponent":null,"competition":null,"match_date":null}
"over 2.5 goals Man City vs Arsenal 20" → {"status":"ok","selection_name":"Over 2.5","event_name":"Man City","sport":"Football","side":"BACK","stake":20,"price":null,"market_type":"OVER_UNDER_25","opponent":"Arsenal","competition":null,"match_date":null}
"first goal scorer Haaland in Man City vs Arsenal 10" → {"status":"ok","selection_name":"Haaland","event_name":"Man City","sport":"Football","side":"BACK","stake":10,"price":null,"market_type":"FIRST_GOAL_SCORER","opponent":"Arsenal","competition":null,"match_date":null}
"first goal scorer Haaland 10" → {"status":"ok","selection_name":"Haaland","event_name":null,"sport":"Football","side":"BACK","stake":10,"price":null,"market_type":"FIRST_GOAL_SCORER","opponent":null,"competition":null,"match_date":null}
"England to win the toss against New Zealand 20" → {"status":"ok","selection_name":"England","event_name":"England","sport":"Cricket","side":"BACK","stake":20,"price":null,"market_type":"TO_WIN_THE_TOSS","opponent":"New Zealand","competition":null,"match_date":null}
"back arsenal" → {"status":"clarification_needed","clarification_question":"How much would you like to stake on Arsenal?","missing_fields":["stake"]}
"back Bayern Munich" → {"status":"clarification_needed","clarification_question":"How much would you like to stake on Bayern Munich?","missing_fields":["stake"]}
"20 quid on football" → {"status":"clarification_needed","clarification_question":"What would you like to bet on?","missing_fields":["selection_name"]}
"a tenner" → {"status":"clarification_needed","clarification_question":"What would you like to bet on?","missing_fields":["selection_name"]}
"back Djokovic at Wimbledon 50" → {"status":"ok","selection_name":"Djokovic","event_name":"Djokovic","sport":"Tennis","side":"BACK","stake":50,"price":null,"market_type":"MATCH_ODDS","opponent":null,"competition":"Wimbledon","match_date":null}
"back Verstappen to win the championship 100" → {"status":"ok","selection_name":"Verstappen","event_name":"Verstappen","sport":"Motorsport","side":"BACK","stake":100,"price":null,"market_type":"OUTRIGHT_WINNER","opponent":null,"competition":null,"match_date":null}
"England to win the World Cup 20" → {"status":"ok","selection_name":"England","event_name":"World Cup","sport":"Football","side":"BACK","stake":20,"price":null,"market_type":"OUTRIGHT_WINNER","opponent":null,"competition":"World Cup","match_date":null}
"back Arsenal to win the Premier League 50" → {"status":"ok","selection_name":"Arsenal","event_name":"Premier League","sport":"Football","side":"BACK","stake":50,"price":null,"market_type":"OUTRIGHT_WINNER","opponent":null,"competition":"Premier League","match_date":null}
"back St. Kilda to win the Premiership 20" → {"status":"ok","selection_name":"St Kilda","event_name":"AFL","sport":"Australian Rules","side":"BACK","stake":20,"price":null,"market_type":"OUTRIGHT_WINNER","opponent":null,"competition":"AFL","match_date":null}
"back Desert Crown each way 25" → {"status":"ok","selection_name":"Desert Crown","event_name":null,"sport":"Horse Racing","side":"BACK","stake":25,"price":null,"market_type":"EACH_WAY","opponent":null,"competition":null,"match_date":null}
"back Constitution Hill 20" → {"status":"ok","selection_name":"Constitution Hill","event_name":null,"sport":"Horse Racing","side":"BACK","stake":20,"price":null,"market_type":"WIN","opponent":null,"competition":null,"match_date":null}
"Desert Crown to place at Ascot 10" → {"status":"ok","selection_name":"Desert Crown","event_name":"Ascot","sport":"Horse Racing","side":"BACK","stake":10,"price":null,"market_type":"PLACE","places":null,"opponent":null,"competition":null,"match_date":null}
"back Kyprios to finish in the top 4 at Ascot 15" → {"status":"ok","selection_name":"Kyprios","event_name":"Ascot","sport":"Horse Racing","side":"BACK","stake":15,"price":null,"market_type":"PLACE","places":4,"opponent":null,"competition":null,"match_date":null}
"50 ante post on Galopin Des Champs for the Gold Cup" → {"status":"ok","selection_name":"Galopin Des Champs","event_name":null,"sport":"Horse Racing","side":"BACK","stake":50,"price":null,"market_type":"ANTEPOST_WIN","opponent":null,"competition":"Gold Cup","match_date":null}
"back Galopin Des Champs to win the Gold Cup 50" → {"status":"ok","selection_name":"Galopin Des Champs","event_name":null,"sport":"Horse Racing","side":"BACK","stake":50,"price":null,"market_type":"WIN","opponent":null,"competition":"Gold Cup","match_date":null}
"back Swift Dancer in the 19:30 at Romford 10" → {"status":"ok","selection_name":"Swift Dancer","event_name":"Romford","sport":"Greyhound Racing","side":"BACK","stake":10,"price":null,"market_type":"WIN","opponent":null,"competition":null,"match_date":"19:30"}
"forecast Mystic Star then Lunar Glow 5" → {"status":"ok","selection_name":"Mystic Star","event_name":null,"sport":"Horse Racing","side":"BACK","stake":5,"price":null,"market_type":"FORECAST","opponent":"Lunar Glow","competition":null,"match_date":null}
"back Rory McIlroy top 5 at the Masters 40" → {"status":"ok","selection_name":"Rory McIlroy","event_name":"Masters","sport":"Golf","side":"BACK","stake":40,"price":null,"market_type":"TOP_5_FINISH","opponent":null,"competition":"Masters","match_date":null}
"back Real Madrid at 2.5 for 50" → {"status":"ok","selection_name":"Real Madrid","event_name":"Real Madrid","sport":"Football","side":"BACK","stake":50,"price":2.5,"market_type":"MATCH_ODDS","opponent":null,"competition":null,"match_date":null}
"back Leinster to win the United Rugby Championship 50" → {"status":"ok","selection_name":"Leinster","event_name":"United Rugby Championship","sport":"Rugby Union","side":"BACK","stake":50,"price":null,"market_type":"OUTRIGHT_WINNER","opponent":null,"competition":"United Rugby Championship","match_date":null}
"back Leinster to beat Bulls 20" → {"status":"ok","selection_name":"Leinster","event_name":"Leinster","sport":"Rugby Union","side":"BACK","stake":20,"price":null,"market_type":"MATCH_ODDS","opponent":"Bulls","competition":null,"match_date":null}
"back under 40.5 total points in Leinster v Bulls 20" → {"status":"ok","selection_name":"Under","event_name":"Leinster","sport":"Rugby Union","side":"BACK","stake":20,"price":null,"market_type":"COMBINED_TOTAL","line":40.5,"opponent":"Bulls","competition":null,"match_date":null}
"back Leinster -7.5 on the handicap against Bulls 20" → {"status":"ok","selection_name":"Leinster","event_name":"Leinster","sport":"Rugby Union","side":"BACK","stake":20,"price":null,"market_type":"HANDICAP","line":-7.5,"opponent":"Bulls","competition":null,"match_date":null}
"back Hurricanes to win by 15+ against Chiefs 20" → {"status":"ok","selection_name":"Hurricanes 15+","event_name":"Hurricanes","sport":"Rugby Union","side":"BACK","stake":20,"price":null,"market_type":"WINNING_MARGIN","opponent":"Chiefs","competition":null,"match_date":null}
"back Northampton to lead at half time and win against Exeter 10" → {"status":"ok","selection_name":"Northampton - Northampton","event_name":"Northampton","sport":"Rugby Union","side":"BACK","stake":10,"price":null,"market_type":"HALF_TIME_FULL_TIME","opponent":"Exeter","competition":null,"match_date":null}
"back Hurricanes head to head against Chiefs 10" → {"status":"ok","selection_name":"Hurricanes","event_name":"Hurricanes","sport":"Rugby Union","side":"BACK","stake":10,"price":null,"market_type":"UNUSED","opponent":"Chiefs","competition":null,"match_date":null}
"""


class AIInterpreter:

    @staticmethod
    def interpret(user_input: str) -> ClarificationResponse:
        try:
            response = _client.beta.chat.completions.parse(
                model="gpt-4o",
                temperature=0,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_input},
                ],
                response_format=BetOutput,
            )

            result: BetOutput = response.choices[0].message.parsed

            _log.info(json.dumps({
                "timestamp": datetime.now(UTC).isoformat(),
                "input": user_input,
                "output": result.model_dump(),
            }))

            if result.status == "clarification_needed":
                return ClarificationResponse(
                    status="clarification_needed",
                    clarification_question=result.clarification_question,
                    missing_fields=result.missing_fields or [],
                )

            if not result.selection_name or result.stake is None:
                raise ValueError("AI returned ok status but missing selection_name or stake")

            bet = ParsedBet(
                selection_name=result.selection_name,
                event_name=result.event_name,
                sport=result.sport or "Football",
                side=result.side or "BACK",
                stake=result.stake,
                price=result.price,
                market_type=result.market_type or "MATCH_ODDS",
                line=result.line,
                places=result.places,
                opponent=result.opponent,
                competition=result.competition,
                match_date=result.match_date,
            )
            return ClarificationResponse(status="ok", parsed_bet=bet)

        except Exception as e:
            _log.info(json.dumps({
                "timestamp": datetime.now(UTC).isoformat(),
                "input": user_input,
                "error": str(e),
            }))
            raise

    @staticmethod
    def select_top_events(
        user_input: str,
        candidates: list,
        market_types: list[str],
        parsed_bet: ParsedBet | None = None,
        n: int = 3,
    ) -> list[dict]:
        """Returns up to n dicts of {event_id, market_type} ranked best to worst.

        `parsed_bet` carries the fields the first model (gpt-4o, with the detailed
        market-type rules) already extracted. We feed them in as a guide so this
        lighter event-picker doesn't re-derive — and second-guess — the market type
        from the raw text (e.g. choosing TOTAL_MATCH_POINTS when the parser already
        determined COMBINED_TOTAL for a single-line total).
        """
        if not candidates:
            return []

        candidates_text = json.dumps(
            [
                {
                    "id": c["event"]["id"],
                    "name": c["event"]["name"],
                    "date": c["event"].get("openDate", ""),
                }
                for c in candidates
            ],
            indent=2,
        )
        market_types_text = json.dumps(market_types)

        parsed_hint = ""
        if parsed_bet is not None:
            already_parsed = {
                "selection_name": parsed_bet.selection_name,
                "event_name": parsed_bet.event_name,
                "sport": parsed_bet.sport,
                "side": parsed_bet.side,
                "market_type": parsed_bet.market_type,
                "line": parsed_bet.line,
                "opponent": parsed_bet.opponent,
                "competition": parsed_bet.competition,
            }
            parsed_hint = f"""

The request has already been parsed into these fields (use them as a strong guide):
{json.dumps(already_parsed, indent=2)}

The parsed "market_type" reflects detailed market-type rules already applied to the
request, so prefer it for every selection whenever it is in the available market
types above. Only pick a different market type if the parsed one is NOT available
(e.g. it is a logical label that maps to a different Betfair code)."""

        response = _client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You select the best matching Betfair event and market type "
                        "for a user's betting request. "
                        "Return ONLY valid JSON. No explanation."
                    ),
                },
                {
                    "role": "user",
                    "content": f"""The user wants to bet: "{user_input}"

Available Betfair events:
{candidates_text}

Available market types:
{market_types_text}{parsed_hint}

Return JSON: {{"selections": [{{"event_id": "<id>", "market_type": "<type>"}}]}}
with up to {n} selections ranked from best to worst match.
- event_id must be from the available events list
- market_type must be from the available market types list
- "UNDIFFERENTIATED" is a rare catch-all — never pick it when a specific market type
  (e.g. NONSPORT for an election, MATCH_ODDS, OUTRIGHT_WINNER) fits the request
- Include only events that could plausibly match the request
- If none match, return {{"selections": []}}
""",
                },
            ],
        )

        content = response.choices[0].message.content
        if content is None:
            return []

        result = json.loads(content.strip())
        selections = [
            s for s in result.get("selections", [])
            if s.get("event_id") and s.get("market_type")
        ]

        # The parser (gpt-4o, with the full market-type rules) is authoritative on
        # the market type whenever it produced a code Betfair actually offers for
        # this sport. Pin it so the lighter event-picker can't substitute a
        # "nicer looking" code — e.g. swapping the rugby head-to-head market
        # (Betfair code UNUSED) for MATCH_ODDS because UNUSED reads like a junk
        # value. When the parsed code is NOT offered (a logical label like the
        # parser's OUTRIGHT_WINNER mapping to Betfair's WINNER) we leave the
        # model's pick untouched.
        if parsed_bet is not None and parsed_bet.market_type in market_types:
            for s in selections:
                s["market_type"] = parsed_bet.market_type

        return selections

    @staticmethod
    def select_market_runner(user_input: str, runners: list) -> int | None:
        """Pick the runner (selectionId) matching a non-racing bet when exact /
        substring name matching failed.

        Some Betfair markets name their runners in ways a parser can't guess and
        that differ between competitions — e.g. WINNING_MARGIN ("Hurricanes 15 +"
        vs "Northampton to win by 15+") or HALF_TIME_FULL_TIME ("Hurricanes -
        Chiefs"). Rather than encode every naming convention, hand the actual
        runner names to the model and let it map the request. Returns the chosen
        selectionId, or None if nothing plausibly matches.
        """
        if not runners:
            return None

        # Dedupe by selectionId (line markets repeat a selectionId across handicaps;
        # the runner name is identical so one entry per id is enough for matching).
        seen = {}
        for r in runners:
            sid = r["selectionId"]
            if sid not in seen:
                seen[sid] = r.get("runnerName", "")
        runners_text = json.dumps(
            [{"selection_id": sid, "name": name} for sid, name in seen.items()],
            indent=2,
        )

        response = _client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You match a user's betting request to the correct runner in "
                        "a betting market. Tolerate differences in wording and format "
                        "(e.g. '15+' vs '15 +' vs 'to win by 15+'). "
                        "Return ONLY valid JSON. No explanation."
                    ),
                },
                {
                    "role": "user",
                    "content": f"""The user wants to bet: "{user_input}"

Available runners in the market:
{runners_text}

Return JSON: {{"selection_id": <selection_id>}} for the runner that best matches the request.
- selection_id must come from the list above.
- If no runner plausibly matches, return {{"selection_id": null}}.
""",
                },
            ],
        )

        content = response.choices[0].message.content
        if content is None:
            return None
        try:
            result = json.loads(content.strip())
            if result.get("selection_id") is not None:
                return int(result["selection_id"])
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
        return None

    @staticmethod
    def select_racing_runner(user_input: str, markets: list) -> dict | None:
        """Pick the race + runner matching a racing bet from one meeting's markets.

        Tier-2 racing fallback: exact runner-name matching found nothing, so let
        the model do typo-tolerant matching over the meeting's actual runner
        names. Returns {"market_id", "selection_id"} or None if nothing plausible.
        """
        if not markets:
            return None

        races_text = json.dumps(
            [
                {
                    "market_id": m["marketId"],
                    "race": m.get("marketName", ""),
                    "start": m.get("marketStartTime", ""),
                    "meeting": m.get("event", {}).get("name", ""),
                    "runners": [
                        {"selection_id": r["selectionId"], "name": r.get("runnerName", "")}
                        for r in m.get("runners", [])
                    ],
                }
                for m in markets
            ],
            indent=2,
        )

        response = _client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You match a user's horse/greyhound racing bet to the correct "
                        "race and runner. The user may have misspelled the runner's "
                        "name. Return ONLY valid JSON. No explanation."
                    ),
                },
                {
                    "role": "user",
                    "content": f"""The user wants to bet: "{user_input}"

Races and their runners:
{races_text}

Return JSON: {{"market_id": "<market_id>", "selection_id": <selection_id>}} for the runner that best matches the request.
- Tolerate typos, partial names, and phonetic spellings.
- market_id and selection_id must come from the list above, from the same race.
- If no runner plausibly matches, return {{"market_id": null, "selection_id": null}}.
""",
                },
            ],
        )

        content = response.choices[0].message.content
        if content is None:
            return None

        # A malformed model response (non-JSON, or a non-numeric selection_id like
        # "12345 (Aperoll)") must degrade to "no pick" so the caller falls through
        # to a clarification — not bubble up as an unhandled 500.
        try:
            result = json.loads(content.strip())
            if result.get("market_id") and result.get("selection_id"):
                return {
                    "market_id": str(result["market_id"]),
                    "selection_id": int(result["selection_id"]),
                }
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
        return None
