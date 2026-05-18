from pydantic import BaseModel
from typing import Optional


class BetRequest(BaseModel):
    user_input: str


class ParsedBet(BaseModel):
    selection_name: str
    sport: str
    side: str
    stake: float
    price: Optional[float]
    market_type: str
    opponent: Optional[str] = None
    competition: Optional[str] = None
    match_date: Optional[str] = None


class PreparedSlip(BaseModel):
    slip_id: str
    event_id: str
    market_id: str
    selection_id: str
    selection_name: str
    side: str
    price: float             # live price fetched from Betfair at time of prepare
    requested_price: Optional[float]  # price the user stated, None if not specified
    stake: float
    projected_return: float