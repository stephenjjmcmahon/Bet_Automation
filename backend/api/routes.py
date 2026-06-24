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
from backend.services.search_tools import SearchTools
from backend.services.search_agent import SearchAgent, classify_intent
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


class SearchFeedbackRequest(BaseModel):
    search_id: int
    correct: bool


class PrepareFromMarketRequest(BaseModel):
    event_id: str
    market_id: str
    selection_id: int
    side: str = "BACK"
    stake: float
    line: Optional[float] = None
    runner_name: Optional[str] = None
    event_name: Optional[str] = None
    competition: Optional[str] = None
    market_type: Optional[str] = None
    event_start_time: Optional[str] = None


class MarketRunnersRequest(BaseModel):
    event_id: str


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


def _persist_slip(
    request: Request,
    request_start: datetime,
    *,
    event_id: str,
    market_id: str,
    selection_id,
    side: str,
    stake: float,
    selection_name: str,
    market_type: Optional[str],
    runner_name: Optional[str] = None,
    event_name: Optional[str] = None,
    competition: Optional[str] = None,
    event_start_time: Optional[str] = None,
    requested_price: Optional[float] = None,
    price_line: Optional[float] = None,
    line: Optional[float] = None,
    places: Optional[int] = None,
    book: Optional[dict] = None,
) -> PreparedSlip:
    """Price-check an already-resolved selection and store it as a PreparedSlip.

    Shared by the natural-language bet path (`_build_slip`) and the search
    "add to slip" path (`/api/prepare-from-market`). `price_line` is the handicap
    used to locate the runner in the book; `line` is what's shown on the slip
    (they differ for `_LINE` markets, where pricing keys off no line but the slip
    still displays the parsed line). Raises MarketSuspendedError /
    InsufficientLiquidityError / ValueError when the market isn't viable.
    """
    live_price = get_best_price(
        market_id, selection_id, side, stake, request.session,
        line=price_line, book=book,
    )

    betslip = create_betslip(market_id, selection_id, side, live_price, stake)

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
        selection_name=selection_name,
        side=side,
        stake=stake,
        price=live_price,
        market_id=market_id,
        event_id=event_id,
        event_start_time=event_start_time,
        time_to_event_seconds=time_to_event_seconds,
    )

    return PreparedSlip(
        slip_id=slip_id,
        event_id=event_id,
        market_id=market_id,
        selection_id=selection_id,
        selection_name=selection_name,
        runner_name=runner_name,
        event_name=event_name,
        competition=competition,
        event_start_time=event_start_time,
        market_type=market_type,
        line=line,
        side=side,
        price=live_price,
        requested_price=requested_price,
        stake=stake,
        projected_return=round(stake * live_price, 2),
        places=places,
    )


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
        return _persist_slip(
            request,
            request_start,
            event_id=market_ids["eventId"],
            market_id=market_ids["marketId"],
            selection_id=market_ids["selectionId"],
            side=effective_side,
            stake=parsed_bet.stake,
            selection_name=parsed_bet.selection_name,
            market_type=chosen_market_type,
            runner_name=market_ids.get("runnerName"),
            event_name=event_name,
            competition=market_ids.get("competition"),
            event_start_time=event_start_time,
            requested_price=parsed_bet.price,
            price_line=effective_line,
            line=parsed_bet.line,
            places=market_ids.get("places"),
            book=market_ids.get("book"),
        )
    except (MarketSuspendedError, InsufficientLiquidityError, ValueError) as e:
        print(f"DEBUG get_best_price failed for {market_ids['marketId']}/{chosen_market_type}: {type(e).__name__}: {e}")
        print()
        return None


def _prepare_slips(request: Request, body: BetRequest) -> list[PreparedSlip]:
    """The natural-language bet pipeline: parse → find event(s) → resolve market →
    price → slip(s). Shared by POST /api/prepare and POST /api/query (bet intent),
    raising the same HTTPExceptions (including the 422 clarification) for both."""
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

        selections = AIInterpreter.select_top_events(
            body.user_input, candidates, market_types, parsed_bet=parsed_bet
        )
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
                market_ids = resolve_market(event_id, parsed_bet, request.session, market_type=chosen_market_type, user_input=body.user_input)
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
    handicap_str = f"  handicap={parsed_bet.line}" if parsed_bet.line is not None else ""
    for i, s in enumerate(slips, 1):
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


# Search refinement context kept in the session: a compact list of {role, content}
# text turns only (no tool calls / card payloads) to stay under the 4 KB cookie cap.
SEARCH_HISTORY_KEY = "search_history"
# UTC ISO timestamp of the last search, used to expire stale refinement context.
SEARCH_HISTORY_AT_KEY = "search_history_at"
MAX_HISTORY_TURNS = 4
# History older than this is treated as a different conversation and dropped, so
# an unrelated search from earlier (or a returning session) can't bias a new one.
SEARCH_HISTORY_TTL_SECONDS = 15 * 60


def _live_search_history(request: Request) -> list:
    """Session search history, or [] if it's older than the TTL (stale thread)."""
    history = request.session.get(SEARCH_HISTORY_KEY, [])
    if not history:
        return []
    last_at = request.session.get(SEARCH_HISTORY_AT_KEY)
    if last_at:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_at)).total_seconds()
        except ValueError:
            age = None
        if age is not None and age > SEARCH_HISTORY_TTL_SECONDS:
            print(f"DEBUG search history expired ({int(age)}s old > {SEARCH_HISTORY_TTL_SECONDS}s) — starting fresh")
            print()
            return []
    return history


@router.post("/api/prepare", response_model=list[PreparedSlip], dependencies=[Depends(_require_session)])
def prepare_bet(request: Request, body: BetRequest):
    return _prepare_slips(request, body)


@router.post("/api/query", dependencies=[Depends(_require_session)])
def query(request: Request, body: BetRequest):
    """Single entry point for the NL box: classify the input and dispatch. Bet
    intent reuses the existing pipeline (same slips, same 422 clarification);
    search intent runs the market-search agent and returns priced cards."""
    intent = classify_intent(body.user_input)
    print(f"DEBUG query intent: {intent}")
    print()

    if intent == "bet":
        slips = _prepare_slips(request, body)
        return {"intent": "bet", "slips": [s.model_dump() for s in slips]}

    history = _live_search_history(request)
    result = SearchAgent.run(body.user_input, request.session, history=history)
    metrics = result.get("metrics")
    search_id = logger.log_search(**metrics) if metrics else None
    request.session[SEARCH_HISTORY_KEY] = (history + [
        {"role": "user", "content": body.user_input},
        {"role": "assistant", "content": result["reply"]},
    ])[-MAX_HISTORY_TURNS:]
    request.session[SEARCH_HISTORY_AT_KEY] = datetime.now(timezone.utc).isoformat()
    return {
        "intent": "search",
        "reply": result["reply"],
        "cards": result["cards"],
        "events": result.get("events", []),
        "search_id": search_id,
    }


@router.post("/api/search/reset")
def reset_search(request: Request):
    """Clear the conversational search history so the next query starts a fresh
    thread. Drives the frontend 'New search' button; no Betfair session needed,
    so it works even after the Betfair token has expired."""
    request.session.pop(SEARCH_HISTORY_KEY, None)
    request.session.pop(SEARCH_HISTORY_AT_KEY, None)
    print("DEBUG search history reset")
    print()
    return {"status": "ok"}


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

    result = place_orders(betslip["marketId"], betslip["instructions"], request.session)

    # Betfair returns HTTP 200 even when it rejects the bet (e.g. stake below the
    # exchange minimum) — the real outcome is in the PlaceExecutionReport body.
    # Don't report success unless the order actually went on, or the user gets a
    # "placed" message for a bet that never reached their account.
    reports = result.get("instructionReports") or []
    placed = result.get("status") == "SUCCESS" and bool(reports) and all(
        r.get("status") == "SUCCESS" for r in reports
    )
    if not placed:
        error_code = (
            (reports[0].get("errorCode") if reports else None)
            or result.get("errorCode")
            or "UNKNOWN"
        )
        print(f"DEBUG placeOrders rejected: status={result.get('status')} errorCode={error_code} body={result}")
        print()
        raise HTTPException(
            status_code=400,
            detail=f"Betfair did not place the bet (code: {error_code}). It has not been placed on your account.",
        )

    return result


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


@router.post("/api/search/feedback")
def search_feedback(body: SearchFeedbackRequest):
    """Record a thumbs up/down on a search result, stored as 1/0 on the matching
    `searches` row. No Betfair session needed — it's a write to our own log DB."""
    logger.log_search_feedback(body.search_id, body.correct)
    return {"status": "ok"}


HEADLINE_MARKETS = 4  # markets priced immediately on event drill-in; rest lazy-load


@router.post("/api/prepare-from-market", response_model=PreparedSlip, dependencies=[Depends(_require_session)])
def prepare_from_market(request: Request, body: PrepareFromMarketRequest):
    """Build a PreparedSlip from a market/selection the user picked in search
    results — no parse/resolve needed. Re-prices live via get_best_price, so the
    slip reflects the current market rather than the indicative search-time price.
    The user then confirms via the unchanged POST /api/confirm/{slip_id}."""
    request_start = datetime.now(timezone.utc)
    try:
        return _persist_slip(
            request,
            request_start,
            event_id=body.event_id,
            market_id=body.market_id,
            selection_id=body.selection_id,
            side=body.side,
            stake=body.stake,
            selection_name=body.runner_name or "",
            market_type=body.market_type,
            runner_name=body.runner_name,
            event_name=body.event_name,
            competition=body.competition,
            event_start_time=body.event_start_time,
            price_line=body.line,
            line=body.line,
        )
    except (MarketSuspendedError, InsufficientLiquidityError) as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/event/{event_id}/markets", dependencies=[Depends(_require_session)])
def event_markets(event_id: str, request: Request):
    """Deterministic browse (no agent): every market for one event, headline ones
    priced now and the rest returned as unpriced headers for lazy pricing on
    expand. Pricing one event is cheap, so this stays well within the budget."""
    tools = SearchTools(request.session)
    markets = tools.list_markets([event_id])
    if not markets:
        raise HTTPException(status_code=404, detail="No markets found for this event.")

    headline_ids = [m["market_id"] for m in markets[:HEADLINE_MARKETS]]
    priced = tools.price_markets(headline_ids)

    first = markets[0]
    return {
        "event_id": event_id,
        "event_name": first["event_name"],
        "competition": first["competition"],
        "market_start_time": first["market_start_time"],
        "priced": priced["markets"],
        "more": [
            {
                "market_id": m["market_id"],
                "market_name": m["market_name"],
                "market_type": m["market_type"],
                "total_matched": m["total_matched"],
            }
            for m in markets[HEADLINE_MARKETS:]
        ],
    }


@router.post("/api/market/{market_id}/runners", dependencies=[Depends(_require_session)])
def market_runners(market_id: str, request: Request, body: MarketRunnersRequest):
    """Lazy-price a single market when its collapsed header is expanded in the
    event view. Needs the event id to repopulate runner-name metadata (the book
    carries only selectionIds) before pricing the one market."""
    tools = SearchTools(request.session)
    tools.list_markets([body.event_id])
    priced = tools.price_markets([market_id])
    if not priced["markets"]:
        raise HTTPException(status_code=404, detail="Market not available.")
    return priced["markets"][0]
