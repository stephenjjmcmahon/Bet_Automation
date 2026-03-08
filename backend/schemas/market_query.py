from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class MarketQuery(BaseModel):
    sport: Optional[str] = None
    competition: Optional[str] = None
    event: Optional[str] = None
    market_type: Optional[str] = None
    selection: Optional[str] = None
    bet_type: Optional[str] = None
    stake: Optional[float] = None
    min_odds: Optional[float] = None
    max_odds: Optional[float] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None