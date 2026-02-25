from pydantic import BaseModel
from typing import Optional


class BetRequest(BaseModel):
    user_input: str


class ParsedBet(BaseModel):
    selection_name: str
    side: str
    stake: float
    price: Optional[float]
    market_type: str