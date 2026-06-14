import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.schemas.bets import BetRequest, PreparedSlip
from backend.services.ai_interpreter import AIInterpreter
from backend.services.search_service import (
    find_event_candidates,
    find_all_events_for_sport,
    resolve_market,
    get_upcoming_fixtures,
    get_market_types,
)
from backend.services.racing_service import (
    resolve_racing_markets,
    RacingClarificationError,
    UnsupportedRacingMarketError,
)
from backend.config.sport_mapping import COMPETITION_SPORTS, RACING_SPORTS
from backend.services.betslips_service import create_betslip
from backend.services.betfair_client import place_orders
from backend.services.odds_service import get_best_price, MarketSuspendedError, InsufficientLiquidityError
from backend.services import pending_slips
from backend.services import logger
from backend.services.betfair_auth import get_token, login, SessionExpiredError


class LoginRequest(BaseModel):
    username: str
    password: str


class FixtureRequest(BaseModel):
    team_name: str
    sport: str = "football"


class ConfirmRequest(BaseModel):
    stake: Optional[float] = None


class FeedbackRequest(BaseModel):
    input: str
    output: dict
    correct: bool
    note: Optional[str] = None


def _require_session(request: Request):
    get_token(request.session)


router = APIRouter()


@router.get("/api/auth/check")
def auth_check(request: Request):
    get_token(request.session)
    return {"status": "ok"}


@router.post("/api/login")
def betfair_login(request: Request, body: LoginRequest):
    try:
        login(body.username, body.password, request.session)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Betfair: {str(e)}")


@router.post("/api/fixtures")
def fixtures(request: Request, body: FixtureRequest):
    try:
        result = get_upcoming_fixtures(body.team_name, body.sport, request.session, limit=3)
        return {"fixtures": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _build_slip(
    request: Request,
    request_start: datetime,
    parsed_bet,
    market_ids: dict,
    chosen_market_type: str,
    event_name: Optional[str],
    event_start_time: Optional[str],
) -> Optional[PreparedSlip]:
    """Price-check a resolved market and turn it into a stored PreparedSlip.

    Returns None when the market isn't viable (suspended / insufficient
    liquidity) so callers can skip to their next candidate.
    """
    if chosen_market_type.endswith("_LINE"):
        effective_side = "BACK" if parsed_bet.selection_name.lower() == "under" else "LAY"
        effective_line = None
    else:
        effective_side = parsed_bet.side
        effective_line = parsed_bet.line

    try:
        live_price = get_best_price(
            market_ids["marketId"],
            market_ids["selectionId"],
            effective_side,
            parsed_bet.stake,
            request.session,
            line=effective_line,
        )
    except (MarketSuspendedError, InsufficientLiquidityError, ValueError) as e:
        print(f"DEBUG get_best_price failed for {market_ids['marketId']}/{chosen_market_type}: {type(e).__name__}: {e}")
        print()
        return None

    betslip = create_betslip(
        market_ids["marketId"],
        market_ids["selectionId"],
        effective_side,
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
        side=effective_side,
        stake=parsed_bet.stake,
        price=live_price,
        market_id=market_ids["marketId"],
        event_id=market_ids["eventId"],
        event_start_time=event_start_time,
        time_to_event_seconds=time_to_event_seconds,
    )

    return PreparedSlip(
        slip_id=slip_id,
        event_id=market_ids["eventId"],
        market_id=market_ids["marketId"],
        selection_id=market_ids["selectionId"],
        selection_name=parsed_bet.selection_name,
        runner_name=market_ids.get("runnerName"),
        event_name=event_name,
        competition=market_ids.get("competition"),
        event_start_time=event_start_time,
        market_type=chosen_market_type,
        side=effective_side,
        price=live_price,
        requested_price=parsed_bet.price,
        stake=parsed_bet.stake,
        projected_return=round(parsed_bet.stake * live_price, 2),
        places=market_ids.get("places"),
    )


@router.post("/api/prepare", response_model=list[PreparedSlip], dependencies=[Depends(_require_session)])
def prepare_bet(request: Request, body: BetRequest):
    request_start = datetime.now(timezone.utc)

    clarification = AIInterpreter.interpret(body.user_input)

    if clarification.status == "clarification_needed":
        raise HTTPException(
            status_code=422,
            detail={
                "status": "clarification_needed",
                "clarification_question": clarification.clarification_question,
                "missing_fields": clarification.missing_fields or [],
                "parsed_bet": clarification.parsed_bet.model_dump() if clarification.parsed_bet else None,
            },
        )

    if clarification.parsed_bet is None:
        raise HTTPException(status_code=500, detail="AI returned ok status but no parsed bet")

    parsed_bet = clarification.parsed_bet
    print(f"DEBUG parsed_bet: {parsed_bet.model_dump()}")
    print()

    slips = []

    if parsed_bet.sport.lower() in RACING_SPORTS:
        # Racing: the race is a market under a meeting event, found
        # deterministically by runner name — no AI event/market pick.
        try:
            matches = resolve_racing_markets(parsed_bet, body.user_input, request.session)
        except UnsupportedRacingMarketError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RacingClarificationError as e:
            raise HTTPException(
                status_code=422,
                detail={
                    "status": "clarification_needed",
                    "clarification_question": e.question,
                    "missing_fields": ["event_name"],
                    "parsed_bet": parsed_bet.model_dump(),
                },
            )

        print(f"DEBUG racing matches: {[(m['eventName'], m['marketId']) for m in matches]}")
        print()

        for market_ids in matches:
            slip = _build_slip(
                request,
                request_start,
                parsed_bet,
                market_ids,
                market_ids["marketType"],
                market_ids.get("eventName"),
                market_ids.get("marketStartTime"),
            )
            if slip:
                slips.append(slip)
    else:
        if parsed_bet.sport.lower() in COMPETITION_SPORTS:
            candidates = find_all_events_for_sport(parsed_bet.sport, request.session)
        else:
            candidates = find_event_candidates(parsed_bet, request.session)

        print(f"DEBUG candidates count: {len(candidates)}")
        print()

        print(f"DEBUG first 3 candidates: {[c['event']['name'] for c in candidates[:3]]}")
        print()

        market_types = get_market_types(parsed_bet.sport, candidates, request.session)
        print(f"DEBUG market_types: {market_types}")
        print()

        selections = AIInterpreter.select_top_events(body.user_input, candidates, market_types)
        print(f"DEBUG selections: {selections}")
        print()

        if not selections:
            logger.log_failure(
                reason="no_matching_event",
                selection_name=parsed_bet.selection_name,
                stake=parsed_bet.stake,
            )
            raise HTTPException(
                status_code=404,
                detail="No matching event found on Betfair. Try including the opponent or competition.",
            )

        for sel in selections:
            event_id = sel["event_id"]
            chosen_market_type = sel["market_type"]
            selected = next((c for c in candidates if c["event"]["id"] == event_id), None)
            event_name = selected["event"].get("name") if selected else None
            event_start_time = selected["event"].get("openDate") if selected else None

            try:
                market_ids = resolve_market(event_id, parsed_bet, request.session, market_type=chosen_market_type)
            except ValueError as e:
                print(f"DEBUG resolve_market failed for {event_id}: {e}")
                print()
                continue

            slip = _build_slip(
                request,
                request_start,
                parsed_bet,
                market_ids,
                chosen_market_type,
                event_name,
                event_start_time,
            )
            if slip:
                slips.append(slip)

    print(f"DEBUG prepared slips ({len(slips)}):")
    for i, s in enumerate(slips, 1):
        handicap_str = f"  handicap={parsed_bet.line}" if parsed_bet.line is not None else ""
        print(f"  {i}.  event={s.event_name}  |  market={s.market_type}  |  runner={s.runner_name or s.selection_name}{handicap_str}  |  {s.side} @ {s.price}")
    print()

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
def confirm_bet(slip_id: str, request: Request, body: ConfirmRequest = ConfirmRequest()):
    print(f"DEBUG confirm session keys: {list(request.session.keys())}")
    print()

    print(f"DEBUG pending_slips in session: {list(request.session.get('pending_slips', {}).keys())}")
    print()

    print(f"DEBUG looking for slip_id: {slip_id}")
    print()
    created_at = pending_slips.get_created_at(request.session, slip_id)
    betslip = pending_slips.pop(request.session, slip_id)

    if betslip is None:
        if created_at is not None:
            logger.log_slip_expired(slip_id)
        raise HTTPException(status_code=404, detail="Slip not found or expired")

    if body.stake is not None:
        betslip["instructions"][0]["limitOrder"]["size"] = body.stake

    now = datetime.now(timezone.utc)
    time_to_confirm_ms = int((now - created_at).total_seconds() * 1000) if created_at else None

    logger.log_bet_confirmed(slip_id=slip_id, time_to_confirm_ms=time_to_confirm_ms)

    return place_orders(betslip["marketId"], betslip["instructions"], request.session)


@router.post("/api/feedback")
def feedback(body: FeedbackRequest):
    Path("logs").mkdir(exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "input": body.input,
        "output": body.output,
        "correct": body.correct,
        "note": body.note or "",
    }
    with open("logs/feedback.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")
    return {"status": "ok"}
