from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from backend.schemas.bets import BetRequest
from backend.services.ai_interpreter import AIInterpreter
from backend.services.search_service import search_market, get_upcoming_fixtures
from backend.api.bet_controller import place_bet
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pydantic import BaseModel as PydanticBase
from typing import Optional
import json
from datetime import datetime
from pathlib import Path

router = APIRouter()
executor = ThreadPoolExecutor(max_workers=8)


class FixtureRequest(BaseModel):
    team_name: str
    sport: str = "football"


@router.post("/api/fixtures")
async def fixtures(request: FixtureRequest):
    """Return next 3 upcoming fixtures for a team — powers the game picker UI."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            executor,
            lambda: get_upcoming_fixtures(request.team_name, request.sport, limit=3)
        )
        return {"fixtures": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/interpret")
async def interpret_bet(request: BetRequest):
    loop = asyncio.get_event_loop()
    try:
        parsed = await loop.run_in_executor(
            executor, AIInterpreter.interpret, request.user_input
        )

        if parsed.status == "clarification_needed":
            return {
                "status": "clarification_needed",
                "clarification_question": parsed.clarification_question,
                "missing_fields": parsed.missing_fields or [],
                "parsed_bet": parsed.parsed_bet.dict() if parsed.parsed_bet else None,
                "market_info": None
            }

        bet = parsed.parsed_bet

        market_info = None
        try:
            raw = await loop.run_in_executor(executor, search_market, bet)
            if raw:
                event_name = raw.get("eventName") or ""
                opponent = raw.get("opponent")
                market_info = {
                    "eventName":   event_name,
                    "opponent":    opponent,
                    "eventDate":   raw.get("eventDate"),
                    "marketId":    raw.get("marketId"),
                    "selectionId": raw.get("selectionId"),
                    "livePrice":   raw.get("livePrice"),
                }
        except Exception:
            pass

        return {
            "status": "ok",
            "parsed_bet": {
                "selection_name": bet.selection_name,
                "sport":         bet.sport,
                "side":          bet.side,
                "stake":         bet.stake,
                "market_type":   bet.market_type,
                "price":         market_info["livePrice"] if market_info else None,
            },
            "clarification_question": None,
            "missing_fields": [],
            "market_info": market_info
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/bet")
async def bet(request: BetRequest):
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(executor, place_bet, request)
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class FeedbackRequest(PydanticBase):
    input: str
    output: dict
    correct: bool
    note: Optional[str] = None

@router.post("/api/feedback")
async def feedback(request: FeedbackRequest):
    Path("logs").mkdir(exist_ok=True)
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "input": request.input,
        "output": request.output,
        "correct": request.correct,
        "note": request.note or ""
    }
    with open("logs/feedback.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")
    return {"status": "ok"}