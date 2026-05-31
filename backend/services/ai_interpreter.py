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
    def select_event(user_input: str, candidates: list) -> str | None:
        """
        Pick the best matching Betfair event from a list of candidates.

        This is a second AI call made after find_event_candidates() returns
        results from Betfair. Rather than blindly taking events[0], we let the
        AI compare the user's original intent against every candidate's name
        and date, then select the most likely match.

        Returns None if candidates is empty or if the AI determines that none
        of the candidates match what the user asked for — the caller should
        treat None as a 404 (no matching event found).

        Parameters
        ----------
        user_input : str
            The raw user input, passed through unchanged so the AI has full
            context rather than just the extracted fields.
        candidates : list
            Raw event list returned by Betfair listEvents. Each element is a
            dict with at minimum {"event": {"id": str, "name": str, "openDate": str}}.

        Returns
        -------
        str | None
            The Betfair event ID of the best match, or None.
        """
        if not candidates:
            return None

        candidates_text = json.dumps(candidates, indent=2)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You select the best matching Betfair event from a list "
                        "based on a user's betting request. "
                        "Return ONLY valid JSON. No explanation."
                    ),
                },
                {
                    "role": "user",
                    "content": f"""The user wants to bet on: "{user_input}"

Available Betfair events:
{candidates_text}

Return JSON: {{"event_id": "<id>"}} with the ID of the best matching event,
or {{"event_id": null}} if none of them match what the user is looking for.
""",
                },
            ],
        )

        content = response.choices[0].message.content
        if content is None:
            return None

        result = json.loads(content.strip())
        print(result)
        return result.get("event_id")
