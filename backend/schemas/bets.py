from typing import Literal

from pydantic import BaseModel


class BetRequest(BaseModel):
    user_input: str


class ParsedBet(BaseModel):
    selection_name: str
    event_name: str | None = None
    sport: str
    side: str
    stake: float
    price: float | None = None
    market_type: str
    line: float | None = None
    places: int | None = None  # racing place bets: number of places to pay (top N), else null
    opponent: str | None = None
    competition: str | None = None
    match_date: str | None = None


class ClarificationResponse(BaseModel):
    status: Literal["ok", "clarification_needed"]
    parsed_bet: ParsedBet | None = None
    clarification_question: str | None = None
    missing_fields: list[str] | None = None


class PreparedSlip(BaseModel):
    slip_id: str
    event_id: str
    market_id: str
    selection_id: int
    selection_name: str
    runner_name: str | None = None
    event_name: str | None = None
    competition: str | None = None
    event_start_time: str | None = None
    market_type: str | None = None
    line: float | None = None  # handicap/total line for line markets (e.g. Under 216.5)
    side: Literal["BACK", "LAY"]
    price: float
    requested_price: float | None = None
    stake: float
    projected_return: float
    places: int | None = None  # racing place/each-way: number of places the market pays
