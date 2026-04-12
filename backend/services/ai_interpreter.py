import os
from openai import OpenAI
from dotenv import load_dotenv
from schemas.bets import ParsedBet
from config.sport_mapping import SPORT_EVENT_TYPE_MAP

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


class AIInterpreter:

    @staticmethod
    def interpret(user_input: str) -> ParsedBet:
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
                    )
                },
                {
                    "role": "user",
                    "content": f"""
Convert the following betting instruction into JSON with fields:
- selection_name (string)
- sport (must be one of: [{allowed_sports}] or if you cannot infer the sport, leave it empty)
- side (must be BACK or LAY)
- stake (number)
- price (number or null)
- market_type (always set to "MATCH_ODDS")

Rules:
- sport must exactly match one of the listed options or be empty if it cannot be inferred.
- Return valid JSON only.

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