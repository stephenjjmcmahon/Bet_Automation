from pydantic import BaseModel

class BetSlip(BaseModel):
    market_id: str
    selection_id: int
    side: str
    price: float
    stake: float