import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

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
    selection_name: Optional[str] = None
    sport: Optional[str] = None
    side: Optional[Literal["BACK", "LAY"]] = None
    stake: Optional[float] = None
    price: Optional[float] = None
    market_type: Optional[str] = None
    opponent: Optional[str] = None
    competition: Optional[str] = None
    match_date: Optional[str] = None
    clarification_question: Optional[str] = None
    missing_fields: Optional[list[str]] = None


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
Rugby team names + "try/six nations/premiership" → Rugby
Political figures + "election/vote/seat/party" → Politics
Default → Football

━━ MARKET TYPE INFERENCE ━━
"to win" / no market specified / match odds → MATCH_ODDS
"btts" / "both teams to score" → BOTH_TEAMS_TO_SCORE
"over 2.5" / "under 2.5" → OVER_UNDER_25 | selection = "Over 2.5" or "Under 2.5"
"over 3.5" / "under 3.5" → OVER_UNDER_35 | selection = "Over 3.5" or "Under 3.5"
"correct score" → CORRECT_SCORE
"first goal scorer" / "first scorer" / "to score first" → FIRST_GOAL_SCORER
"asian handicap" → ASIAN_HANDICAP
"draw no bet" / "dnb" → DRAW_NO_BET
"half time" / "ht result" → HALF_TIME
"each way" / "ew" → EACH_WAY
"to place" (horse racing) → PLACE
"to win the race" / "win" (horse racing) → WIN
"top 5" (golf) → TOP_5_FINISH | "top 10" (golf) → TOP_10_FINISH
"to win the tournament" / "tournament winner" / "outright" → OUTRIGHT_WINNER
"make the cut" (golf) → MAKE_CUT
"method of victory" → METHOD_OF_VICTORY
"round betting" → ROUND_BETTING
"championship" / "title" (F1/sport season) → OUTRIGHT_WINNER
"top batsman" → TOP_BATSMAN | "top bowler" → TOP_BOWLER
"map winner" (esports) → MAP_WINNER

━━ ADDITIONAL EXTRACTION ━━
- opponent: the opposing team or player if mentioned (e.g. "vs Arsenal" → "Arsenal"), else null
- competition: the league or tournament if mentioned (e.g. "Premier League", "US Open"), else null
- match_date: any date or time reference if mentioned (e.g. "Saturday", "tonight", "3pm"), else null

━━ EXAMPLES (follow these exactly) ━━
"back Man City to win 20" → {"status":"ok","selection_name":"Man City","sport":"Football","side":"BACK","stake":20,"price":null,"market_type":"MATCH_ODDS","opponent":null,"competition":null,"match_date":null}
"lay Liverpool a tenner" → {"status":"ok","selection_name":"Liverpool","sport":"Football","side":"LAY","stake":10,"price":null,"market_type":"MATCH_ODDS","opponent":null,"competition":null,"match_date":null}
"Man City vs Arsenal 20" → {"status":"ok","selection_name":"Man City","sport":"Football","side":"BACK","stake":20,"price":null,"market_type":"MATCH_ODDS","opponent":"Arsenal","competition":null,"match_date":null}
"back Man City in the Premier League 20" → {"status":"ok","selection_name":"Man City","sport":"Football","side":"BACK","stake":20,"price":null,"market_type":"MATCH_ODDS","opponent":null,"competition":"Premier League","match_date":null}
"back Man City tonight 20" → {"status":"ok","selection_name":"Man City","sport":"Football","side":"BACK","stake":20,"price":null,"market_type":"MATCH_ODDS","opponent":null,"competition":null,"match_date":"tonight"}
"back arsenal" → {"status":"clarification_needed","clarification_question":"How much would you like to stake on Arsenal?","missing_fields":["stake"]}
"back Bayern Munich" → {"status":"clarification_needed","clarification_question":"How much would you like to stake on Bayern Munich?","missing_fields":["stake"]}
"20 quid on football" → {"status":"clarification_needed","clarification_question":"What would you like to bet on?","missing_fields":["selection_name"]}
"a tenner" → {"status":"clarification_needed","clarification_question":"What would you like to bet on?","missing_fields":["selection_name"]}
"back Djokovic at Wimbledon 50" → {"status":"ok","selection_name":"Djokovic","sport":"Tennis","side":"BACK","stake":50,"price":null,"market_type":"MATCH_ODDS","opponent":null,"competition":"Wimbledon","match_date":null}
"back Verstappen to win the championship 100" → {"status":"ok","selection_name":"Verstappen","sport":"Motorsport","side":"BACK","stake":100,"price":null,"market_type":"OUTRIGHT_WINNER","opponent":null,"competition":null,"match_date":null}
"back Desert Crown each way 25" → {"status":"ok","selection_name":"Desert Crown","sport":"Horse Racing","side":"BACK","stake":25,"price":null,"market_type":"EACH_WAY","opponent":null,"competition":null,"match_date":null}
"back Rory McIlroy top 5 at the Masters 40" → {"status":"ok","selection_name":"Rory McIlroy","sport":"Golf","side":"BACK","stake":40,"price":null,"market_type":"TOP_5_FINISH","opponent":null,"competition":"Masters","match_date":null}
"over 2.5 goals Man City vs Arsenal 20" → {"status":"ok","selection_name":"Over 2.5","sport":"Football","side":"BACK","stake":20,"price":null,"market_type":"OVER_UNDER_25","opponent":null,"competition":null,"match_date":null}
"back Real Madrid at 2.5 for 50" → {"status":"ok","selection_name":"Real Madrid","sport":"Football","side":"BACK","stake":50,"price":2.5,"market_type":"MATCH_ODDS","opponent":null,"competition":null,"match_date":null}
"first goal scorer Haaland 10" → {"status":"ok","selection_name":"Haaland","sport":"Football","side":"BACK","stake":10,"price":null,"market_type":"FIRST_GOAL_SCORER","opponent":null,"competition":null,"match_date":null}
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
                "timestamp": datetime.utcnow().isoformat(),
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
                sport=result.sport or "Football",
                side=result.side or "BACK",
                stake=result.stake,
                price=result.price,
                market_type=result.market_type or "MATCH_ODDS",
                opponent=result.opponent,
                competition=result.competition,
                match_date=result.match_date,
            )
            return ClarificationResponse(status="ok", parsed_bet=bet)

        except Exception as e:
            _log.info(json.dumps({
                "timestamp": datetime.utcnow().isoformat(),
                "input": user_input,
                "error": str(e),
            }))
            raise

    @staticmethod
    def select_top_events(user_input: str, candidates: list, n: int = 3) -> list[str]:
        if not candidates:
            return []

        candidates_text = json.dumps(candidates, indent=2)

        response = _client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You select the best matching Betfair events from a list "
                        "based on a user's betting request. "
                        "Return ONLY valid JSON. No explanation."
                    ),
                },
                {
                    "role": "user",
                    "content": f"""The user wants to bet on: "{user_input}"

Available Betfair events:
{candidates_text}

Return JSON: {{"event_ids": ["<id1>", "<id2>", "<id3>"]}} with up to {n} event IDs
ranked from best to worst match. Include only events that could plausibly match the request.
Return fewer IDs if fewer events match. If none match, return {{"event_ids": []}}.
""",
                },
            ],
        )

        content = response.choices[0].message.content
        if content is None:
            return []

        result = json.loads(content.strip())
        return [eid for eid in result.get("event_ids", []) if eid is not None]
