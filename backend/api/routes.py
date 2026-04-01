from fastapi import APIRouter
from backend.schemas.bets import BetRequest, ParsedBet
from backend.services.ai_interpreter import AIInterpreter

router = APIRouter()


@router.post("/api/interpret", response_model=ParsedBet)
def interpret_bet(request: BetRequest):
    return AIInterpreter.interpret(request.user_input)