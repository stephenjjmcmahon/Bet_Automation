import os
from fastapi import FastAPI, Body
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
app = FastAPI()

class BetRequest(BaseModel):
    user_input: str

class ParsedBet(BaseModel):
    selection_name: str
    side: str
    stake: float
    price: Optional[float]
    market_type: str

#Routes 
@app.post("/api/interpret", response_model=ParsedBet)
def interpret_bet(request: BetRequest):

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
- side (must be BACK or LAY)
- stake (number)
- price (number or null)
- market_type (always set to "MATCH_ODDS")

User input:
"{request.user_input}"
"""
            }
        ],
    )

    content = response.choices[0].message.content
    if content is None:
        raise ValueError("AI returned empty response")
    ai_output = content.strip()


    # Validate AI output against schema
    return ParsedBet.model_validate_json(ai_output)



