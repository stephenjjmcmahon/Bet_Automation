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
    event_name: Optional[str] = None       # full Betfair event name e.g. "Chelsea v Man City"
    competition: Optional[str] = None      # from Betfair COMPETITION projection
    event_start_time: Optional[str] = None # ISO start time from Betfair
    side: str
    price: float             # live price fetched from Betfair at time of prepare
    requested_price: Optional[float]  # price the user stated, None if not specified
    stake: float
    projected_return: float
