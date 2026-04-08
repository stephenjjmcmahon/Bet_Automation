from pydantic import BaseModel
from typing import Optional, Literal

class BetRequest(BaseModel):
    user_input: str

class ParsedBet(BaseModel):
    selection_name: str
    sport: str
    side: str
    stake: float
    price: Optional[float]
    market_type: str

class ClarificationResponse(BaseModel):
    status: Literal["ok", "clarification_needed"]
    parsed_bet: Optional[ParsedBet] = None
    clarification_question: Optional[str] = None
    missing_fields: Optional[list[str]] = None