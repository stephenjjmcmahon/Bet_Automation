from fastapi import APIRouter, HTTPException
from backend.schemas.bets import BetRequest, ClarificationResponse
from backend.services.ai_interpreter import AIInterpreter

router = APIRouter()

@router.post("/api/interpret", response_model=ClarificationResponse)
def interpret_bet(request: BetRequest):
    try:
        return AIInterpreter.interpret(request.user_input)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))