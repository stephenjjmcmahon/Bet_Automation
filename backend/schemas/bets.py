from typing import Literal, Optional

from pydantic import BaseModel


class BetRequest(BaseModel):
    user_input: str


class ParsedBet(BaseModel):
    selection_name: str
    event_name: Optional[str] = None
    sport: str
    side: str
    stake: float
    price: Optional[float] = None
    market_type: str
    line: Optional[float] = None
    places: Optional[int] = None  # racing place bets: number of places to pay (top N), else null
    opponent: Optional[str] = None
    competition: Optional[str] = None
    match_date: Optional[str] = None


class ClarificationResponse(BaseModel):
    status: Literal["ok", "clarification_needed"]
    parsed_bet: Optional[ParsedBet] = None
    clarification_question: Optional[str] = None
    missing_fields: Optional[list[str]] = None


class PreparedSlip(BaseModel):
    slip_id: str
    event_id: str
    market_id: str
    selection_id: int
    selection_name: str
    runner_name: Optional[str] = None
    event_name: Optional[str] = None
    competition: Optional[str] = None
    event_start_time: Optional[str] = None
    market_type: Optional[str] = None
    line: Optional[float] = None  # handicap/total line for line markets (e.g. Under 216.5)
    side: Literal["BACK", "LAY"]
    price: float
    requested_price: Optional[float] = None
    stake: float
    projected_return: float
    places: Optional[int] = None  # racing place/each-way: number of places the market pays
