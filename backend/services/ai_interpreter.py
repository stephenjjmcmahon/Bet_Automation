import os
import json
from openai import OpenAI
from dotenv import load_dotenv
from backend.schemas.bets import ParsedBet, ClarificationResponse

load_dotenv()

class AIInterpreter:

    @staticmethod
    def interpret(user_input: str) -> ClarificationResponse:

        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You extract structured betting instructions from natural language. "
                        "Return ONLY valid JSON. No explanation. No markdown.\n\n"
                        "If the input is clear and complete, return:\n"
                        "{\n"
                        '  "status": "ok",\n'
                        '  "selection_name": string,\n'
                        '  "sport": string,\n'
                        '  "side": "BACK" or "LAY",\n'
                        '  "stake": number,\n'
                        '  "price": number or null,\n'
                        '  "market_type": "MATCH_ODDS",\n'
                        '  "missing_fields": []\n'
                        "}\n\n"
                        "If the input is ambiguous or missing required information, return:\n"
                        "{\n"
                        '  "status": "clarification_needed",\n'
                        '  "clarification_question": "A single clear question to resolve the ambiguity",\n'
                        '  "missing_fields": ["field1", "field2"]\n'
                        "}\n\n"
                        "Required fields are: selection_name, side, stake.\n"
                        "Treat missing stake, missing selection, or ambiguous team names as clarification_needed.\n"
                        "If side is not specified, assume BACK — do not ask.\n"
                        "If market_type is not specified, use MATCH_ODDS — do not ask.\n"
                        "If price is not specified, set it to null — do not ask."
                    )
                },
                {
                    "role": "user",
                    "content": f'Betting instruction: "{user_input}"'
                }
            ],
        )

        content = response.choices[0].message.content
        if content is None:
            raise ValueError("AI returned empty response")

        data = json.loads(content.strip())
        status = data.get("status", "ok")

        if status == "clarification_needed":
            return ClarificationResponse(
                status="clarification_needed",
                clarification_question=data.get("clarification_question"),
                missing_fields=data.get("missing_fields", [])
            )

        bet = ParsedBet(
            selection_name=data["selection_name"],
            sport=data["sport"],
            side=data["side"],
            stake=data["stake"],
            price=data.get("price"),
            market_type=data.get("market_type", "MATCH_ODDS")
        )
        return ClarificationResponse(status="ok", parsed_bet=bet)