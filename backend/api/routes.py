from fastapi import APIRouter, HTTPException
from backend.schemas.bets import BetRequest
from backend.services.ai_interpreter import AIInterpreter
from backend.services.search_service import search_market
from backend.api.bet_controller import place_bet

router = APIRouter()


@router.post("/api/interpret")
def interpret_bet(request: BetRequest):
    try:
        # Step 1: Parse natural language
        parsed = AIInterpreter.interpret(request.user_input)

        if parsed.status == "clarification_needed":
            return {
                "status": "clarification_needed",
                "clarification_question": parsed.clarification_question,
                "parsed_bet": None,
                "market_info": None
            }

        bet = parsed.parsed_bet

        # Step 2: Look up Betfair market to get full event details + live price
        market_info = None
        try:
            raw = search_market(bet)
            if raw:
                event_name = raw.get("eventName") or raw.get("event_name") or ""
                selection = bet.selection_name

                opponent = None
                if " v " in event_name:
                    home, away = event_name.split(" v ", 1)
                    opponent = away if selection.lower() in home.lower() else home

                market_info = {
                    "eventName":   event_name,
                    "opponent":    opponent,
                    "eventDate":   raw.get("eventDate") or raw.get("event_date"),
                    "marketId":    raw.get("marketId"),
                    "selectionId": raw.get("selectionId"),
                    "livePrice":   raw.get("livePrice"),
                }
        except Exception:
            pass  # market_info stays None — slip still renders fine

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
            "market_info": market_info
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))