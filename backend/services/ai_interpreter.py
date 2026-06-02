import json
import os
from openai import OpenAI
from dotenv import load_dotenv
from backend.schemas.bets import ParsedBet
from backend.config.sport_mapping import SPORT_EVENT_TYPE_MAP

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


class AIInterpreter:

    @staticmethod
    def interpret(user_input: str) -> ParsedBet:
        """
        Parse a natural language betting instruction into a structured ParsedBet.

        Calls GPT-4o-mini with temperature=0 (deterministic output) and instructs
        it to return only JSON matching the ParsedBet schema. The optional fields
        (opponent, competition, match_date) are extracted when mentioned but left
        null if the user did not specify them.
        """
        allowed_sports = ", ".join(f'"{sport}"' for sport in SPORT_EVENT_TYPE_MAP.keys())

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You extract structured betting instructions. "
                        "Return ONLY valid JSON. No explanation."
                    ),
                },
                {
                    "role": "user",
                    "content": f"""
Convert the following betting instruction into JSON with these fields:
- selection_name (string): the team or player being bet on
- opponent (string or null): the opposing team or player, if mentioned
- competition (string or null): the league or tournament, if mentioned (e.g. "Premier League")
- match_date (string or null): any date or time reference if mentioned (e.g. "tonight", "Saturday"), as a raw string
- sport (must be one of: {allowed_sports}, or empty string if it cannot be inferred)
- side (must be "BACK" or "LAY")
- stake (number)
- price (number or null): the odds if explicitly stated, otherwise null
- market_type (always set to "MATCH_ODDS")


User input:
"{user_input}"
"""
                }
            ],
        )

        content = response.choices[0].message.content
        if content is None:
            raise ValueError("AI returned empty response")

        return ParsedBet.model_validate_json(content.strip())

    @staticmethod
    def select_top_events(user_input: str, candidates: list, n: int = 3) -> list[str]:
        """
        Pick the top n most likely matching Betfair events, ranked best-to-worst.

        Returns an empty list if candidates is empty or the AI finds no matches.
        """
        if not candidates:
            return []

        candidates_text = json.dumps(candidates, indent=2)

        response = client.chat.completions.create(
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
