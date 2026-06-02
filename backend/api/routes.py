from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.schemas.bets import BetRequest, PreparedSlip
from backend.services.ai_interpreter import AIInterpreter
from backend.services.search_service import find_event_candidates, resolve_market
from backend.services.betslips_service import create_betslip
from backend.services.betfair_client import place_orders
from backend.services.odds_service import get_best_price, MarketSuspendedError, InsufficientLiquidityError
from backend.services import pending_slips
from backend.services import logger
from backend.services.betfair_auth import get_token, login, SessionExpiredError


class LoginRequest(BaseModel):
    username: str
    password: str


def _require_session(request: Request):
    get_token(request.session)


router = APIRouter()


@router.get("/api/auth/check")
def auth_check(request: Request):
    # Raises SessionExpiredError (→ 401) if no token in session, otherwise 200.
    get_token(request.session)
    return {"status": "ok"}


@router.post("/api/login")
def betfair_login(request: Request, body: LoginRequest):
    try:
        login(body.username, body.password, request.session)
        return {"status": "ok"}
    except ValueError as e:
        # Betfair returned a non-SUCCESS status (wrong credentials, locked account, etc.)
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        # Network failure or unexpected error reaching the Betfair auth endpoint
        raise HTTPException(status_code=502, detail=f"Could not reach Betfair: {str(e)}")


@router.post("/api/prepare", response_model=list[PreparedSlip], dependencies=[Depends(_require_session)])
def prepare_bet(request: Request, body: BetRequest):
    request_start = datetime.now(timezone.utc)

    parsed_bet = AIInterpreter.interpret(body.user_input)

    candidates = find_event_candidates(parsed_bet, request.session)
    event_ids = AIInterpreter.select_top_events(body.user_input, candidates)

    if not event_ids:
        logger.log_failure(
            reason="no_matching_event",
            selection_name=parsed_bet.selection_name,
            stake=parsed_bet.stake,
        )
        raise HTTPException(
            status_code=404,
            detail="No matching event found on Betfair. Try including the opponent or competition.",
        )

    slips = []
    for event_id in event_ids:
        selected = next((c for c in candidates if c["event"]["id"] == event_id), None)
        event_name = selected["event"].get("name") if selected else None
        event_start_time = selected["event"].get("openDate") if selected else None

        try:
            market_ids = resolve_market(event_id, parsed_bet, request.session)
        except ValueError:
            continue

        try:
            live_price = get_best_price(
                market_ids["marketId"],
                market_ids["selectionId"],
                parsed_bet.side,
                parsed_bet.stake,
                request.session,
            )
        except (MarketSuspendedError, InsufficientLiquidityError):
            continue

        betslip = create_betslip(
            market_ids["marketId"],
            market_ids["selectionId"],
            parsed_bet.side,
            live_price,
            parsed_bet.stake,
        )

        slip_id = str(uuid4())
        pending_slips.save(request.session, slip_id, betslip)

        now = datetime.now(timezone.utc)
        time_to_slip_ms = int((now - request_start).total_seconds() * 1000)

        time_to_event_seconds = None
        if event_start_time:
            try:
                start_dt = datetime.fromisoformat(event_start_time.replace("Z", "+00:00"))
                time_to_event_seconds = int((start_dt - now).total_seconds())
            except (ValueError, TypeError):
                pass

        logger.log_slip_prepared(
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

        slips.append(PreparedSlip(
            slip_id=slip_id,
            event_id=market_ids["eventId"],
            market_id=market_ids["marketId"],
            selection_id=market_ids["selectionId"],
            selection_name=parsed_bet.selection_name,
            event_name=event_name,
            competition=market_ids.get("competition"),
            event_start_time=event_start_time,
            side=parsed_bet.side,
            price=live_price,
            requested_price=parsed_bet.price,
            stake=parsed_bet.stake,
            projected_return=round(parsed_bet.stake * live_price, 2),
        ))

    if not slips:
        logger.log_failure(
            reason="market_resolution_failed",
            selection_name=parsed_bet.selection_name,
            stake=parsed_bet.stake,
        )
        raise HTTPException(
            status_code=404,
            detail="No matching event found on Betfair. Try including the opponent or competition.",
        )

    return slips


@router.post("/api/confirm/{slip_id}", dependencies=[Depends(_require_session)])
def confirm_bet(slip_id: str, request: Request):
    created_at = pending_slips.get_created_at(request.session, slip_id)
    betslip = pending_slips.pop(request.session, slip_id)

    if betslip is None:
        if created_at is not None:
            # Entry existed in the session but the TTL had passed
            logger.log_slip_expired(slip_id)
        raise HTTPException(status_code=404, detail="Slip not found or expired")

    now = datetime.now(timezone.utc)
    time_to_confirm_ms = int((now - created_at).total_seconds() * 1000) if created_at else None

    logger.log_bet_confirmed(slip_id=slip_id, time_to_confirm_ms=time_to_confirm_ms)

    return place_orders(betslip["marketId"], betslip["instructions"], request.session)
