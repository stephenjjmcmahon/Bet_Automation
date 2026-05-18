from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from backend.schemas.bets import BetRequest, ParsedBet, PreparedSlip
from backend.services.ai_interpreter import AIInterpreter
from backend.services.search_service import find_event_candidates, resolve_market
from backend.services.betslips_service import create_betslip
from backend.services.betfair_client import place_orders
from backend.services.odds_service import get_best_price, MarketSuspendedError, InsufficientLiquidityError
from backend.services import pending_slips
from backend.services import logger

router = APIRouter()


@router.post("/api/interpret", response_model=ParsedBet)
def interpret_bet(request: BetRequest):
    return AIInterpreter.interpret(request.user_input)


@router.post("/api/prepare", response_model=PreparedSlip)
def prepare_bet(request: BetRequest):
    request_start = datetime.now(timezone.utc)

    parsed_bet = AIInterpreter.interpret(request.user_input)

    candidates = find_event_candidates(parsed_bet)
    event_id = AIInterpreter.select_event(request.user_input, candidates)

    if event_id is None:
        logger.log_event(
            "prepare_failed",
            reason="no_matching_event",
            selection_name=parsed_bet.selection_name,
            stake=parsed_bet.stake,
        )
        raise HTTPException(
            status_code=404,
            detail="No matching event found on Betfair. Try including the opponent or competition.",
        )

    # Extract the selected event's start time from the candidate list for time_to_event logging
    selected = next((c for c in candidates if c["event"]["id"] == event_id), None)
    event_start_time = selected["event"].get("openDate") if selected else None

    try:
        market_ids = resolve_market(event_id, parsed_bet)
    except ValueError as e:
        logger.log_event(
            "prepare_failed",
            reason="market_resolution_failed",
            selection_name=parsed_bet.selection_name,
            stake=parsed_bet.stake,
            event_id=event_id,
        )
        raise HTTPException(status_code=404, detail=str(e))

    try:
        live_price = get_best_price(
            market_ids["marketId"],
            market_ids["selectionId"],
            parsed_bet.side,
            parsed_bet.stake,
        )
    except MarketSuspendedError as e:
        logger.log_event(
            "prepare_failed",
            reason="market_suspended",
            selection_name=parsed_bet.selection_name,
            stake=parsed_bet.stake,
            market_id=market_ids["marketId"],
        )
        raise HTTPException(status_code=503, detail=str(e))
    except InsufficientLiquidityError as e:
        logger.log_event(
            "prepare_failed",
            reason="insufficient_liquidity",
            selection_name=parsed_bet.selection_name,
            stake=parsed_bet.stake,
            market_id=market_ids["marketId"],
        )
        raise HTTPException(status_code=409, detail=str(e))

    betslip = create_betslip(
        market_ids["marketId"],
        market_ids["selectionId"],
        parsed_bet.side,
        live_price,
        parsed_bet.stake,
    )

    slip_id = str(uuid4())
    pending_slips.save(slip_id, betslip)

    now = datetime.now(timezone.utc)
    time_to_slip_ms = int((now - request_start).total_seconds() * 1000)

    time_to_event_seconds = None
    if event_start_time:
        try:
            start_dt = datetime.fromisoformat(event_start_time.replace("Z", "+00:00"))
            time_to_event_seconds = int((start_dt - now).total_seconds())
        except (ValueError, TypeError):
            pass

    logger.log_event(
        "slip_prepared",
        slip_id=slip_id,
        time_to_slip_ms=time_to_slip_ms,
        selection_name=parsed_bet.selection_name,
        side=parsed_bet.side,
        stake=parsed_bet.stake,
        price=live_price,
        market_id=market_ids["marketId"],
        event_id=event_id,
        event_start_time=event_start_time,
        time_to_event_seconds=time_to_event_seconds,
    )

    return PreparedSlip(
        slip_id=slip_id,
        event_id=market_ids["eventId"],
        market_id=market_ids["marketId"],
        selection_id=market_ids["selectionId"],
        selection_name=parsed_bet.selection_name,
        side=parsed_bet.side,
        price=live_price,
        requested_price=parsed_bet.price,
        stake=parsed_bet.stake,
        projected_return=round(parsed_bet.stake * live_price, 2),
    )


@router.post("/api/confirm/{slip_id}")
def confirm_bet(slip_id: str):
    created_at = pending_slips.get_created_at(slip_id)
    betslip = pending_slips.pop(slip_id)

    if betslip is None:
        if created_at is not None:
            # Entry existed in the store but the TTL had passed
            logger.log_event("slip_expired", slip_id=slip_id)
        raise HTTPException(status_code=404, detail="Slip not found or expired")

    now = datetime.now(timezone.utc)
    time_to_confirm_ms = int((now - created_at).total_seconds() * 1000)
    limit_order = betslip["instructions"][0]["limitOrder"]

    logger.log_event(
        "bet_confirmed",
        slip_id=slip_id,
        time_to_confirm_ms=time_to_confirm_ms,
        stake=limit_order["size"],
        price=limit_order["price"],
        market_id=betslip["marketId"],
    )

    return place_orders(betslip["marketId"], betslip["instructions"])
