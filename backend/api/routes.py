import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.rate_limit import AI_RATE_LIMIT, LOGIN_RATE_LIMIT, limiter
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

log = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    username: str
    password: str


class FixtureRequest(BaseModel):
    team_name: str
    sport: str = "football"


# Hard ceiling on any single stake, in GBP. This is a safety rail on a client-supplied
# value that ends up as a real order on a real exchange — keep the default conservative.
MAX_STAKE_GBP = float(os.getenv("MAX_STAKE_GBP", "100"))


class ConfirmRequest(BaseModel):
    stake: Optional[float] = Field(default=None, gt=0, le=MAX_STAKE_GBP)


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
    side: Literal["BACK", "LAY"] = "BACK"
    stake: float = Field(gt=0, le=MAX_STAKE_GBP)
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
@limiter.limit(LOGIN_RATE_LIMIT)
def betfair_login(request: Request, body: LoginRequest):
    try:
        login(body.username, body.password, request.session)
        return {"status": "ok"}
    except ValueError as e:
        # Betfair's own rejection reason (bad password, account locked) — safe to pass on.
        raise HTTPException(status_code=401, detail=str(e))
    except Exception:
        # Anything else is our side of the wire. Log the detail, return a generic
        # message rather than leaking internal error text to the client.
        log.exception("Betfair login request failed")
        raise HTTPException(status_code=502, detail="Could not reach Betfair. Please try again.")


@router.post("/api/fixtures", dependencies=[Depends(_require_session)])
def fixtures(request: Request, body: FixtureRequest):
    try:
        result = get_upcoming_fixtures(body.team_name, body.sport, request.session, limit=3)
        return {"fixtures": result}
    except SessionExpiredError:
        # Must reach the global handler as a 401 or the frontend never re-shows login.
        raise
    except Exception:
        log.exception("Fixture lookup failed for %s", body.team_name)
        raise HTTPException(status_code=500, detail="Could not load fixtures.")


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
    # Local bookkeeping, not part of the Betfair payload (place_orders only reads
    # "marketId" and "instructions"). Confirm-time re-pricing needs the same
    # handicap line used here, or it would match the wrong row of a line market.
    betslip["_priceLine"] = price_line

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
        log.warning(
            "Market %s/%s not viable, skipping: %s: %s",
            market_ids["marketId"], chosen_market_type, type(e).__name__, e,
        )
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
    # Debug level: this echoes the user's bet, so it stays off by default.
    log.debug("parsed_bet: %s", parsed_bet.model_dump())

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

        if log.isEnabledFor(logging.DEBUG):
            log.debug("racing matches: %s", [(m["eventName"], m["marketId"]) for m in matches])

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

        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "%d candidate event(s); first 3: %s",
                len(candidates), [c["event"]["name"] for c in candidates[:3]],
            )

        market_types = get_market_types(parsed_bet.sport, candidates, request.session)
        log.debug("available market types: %s", market_types)

        selections = AIInterpreter.select_top_events(
            body.user_input, candidates, market_types, parsed_bet=parsed_bet
        )
        log.debug("AI selections: %s", selections)

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
                log.warning("resolve_market failed for event %s: %s", event_id, e)
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

    log.info("Prepared %d slip(s) for %r", len(slips), body.user_input)
    if log.isEnabledFor(logging.DEBUG):
        handicap_str = f" handicap={parsed_bet.line}" if parsed_bet.line is not None else ""
        for i, s in enumerate(slips, 1):
            log.debug(
                "  %d. event=%s | market=%s | runner=%s%s | %s @ %s",
                i, s.event_name, s.market_type,
                s.runner_name or s.selection_name, handicap_str, s.side, s.price,
            )

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
            log.debug(
                "search history expired (%ds old > %ds) — starting fresh",
                int(age), SEARCH_HISTORY_TTL_SECONDS,
            )
            return []
    return history


@router.post("/api/prepare", response_model=list[PreparedSlip], dependencies=[Depends(_require_session)])
@limiter.limit(AI_RATE_LIMIT)
def prepare_bet(request: Request, body: BetRequest):
    return _prepare_slips(request, body)


@router.post("/api/query", dependencies=[Depends(_require_session)])
@limiter.limit(AI_RATE_LIMIT)
def query(request: Request, body: BetRequest):
    """Single entry point for the NL box: classify the input and dispatch. Bet
    intent reuses the existing pipeline (same slips, same 422 clarification);
    search intent runs the market-search agent and returns priced cards."""
    intent = classify_intent(body.user_input)
    log.info("Query classified as %r intent", intent)

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
    log.debug("search history reset")
    return {"status": "ok"}


@router.post("/api/confirm/{slip_id}", dependencies=[Depends(_require_session)])
def confirm_bet(slip_id: str, request: Request, body: ConfirmRequest = ConfirmRequest()):
    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "confirm %s — session keys=%s, pending slips=%s",
            slip_id, list(request.session.keys()),
            list(request.session.get("pending_slips", {}).keys()),
        )
    created_at = pending_slips.get_created_at(request.session, slip_id)
    betslip = pending_slips.pop(request.session, slip_id)

    if betslip is None:
        if created_at is not None:
            logger.log_slip_expired(slip_id)
        raise HTTPException(status_code=404, detail="Slip not found or expired")

    instruction = betslip["instructions"][0]

    # The frontend lets the user edit the stake box between prepare and confirm, so
    # the stake that reaches Betfair may not be the one get_best_price validated at
    # prepare time. Re-run the liquidity gate against the live book for the new
    # amount rather than trusting a client-supplied size — otherwise a £2 slip that
    # passed the gate can be confirmed at the MAX_STAKE_GBP ceiling unchecked.
    if body.stake is not None and body.stake != instruction["limitOrder"]["size"]:
        try:
            live_price = get_best_price(
                betslip["marketId"],
                instruction["selectionId"],
                instruction["side"],
                body.stake,
                request.session,
                line=betslip.get("_priceLine"),
            )
        except (MarketSuspendedError, InsufficientLiquidityError) as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        instruction["limitOrder"]["size"] = body.stake
        # The stored price is stale by now — the re-fetched book is the honest one.
        instruction["limitOrder"]["price"] = live_price

    now = datetime.now(timezone.utc)
    time_to_confirm_ms = int((now - created_at).total_seconds() * 1000) if created_at else None

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
        log.warning(
            "placeOrders rejected slip %s: status=%s errorCode=%s body=%s",
            slip_id, result.get("status"), error_code, result,
        )
        raise HTTPException(
            status_code=400,
            detail=f"Betfair did not place the bet (code: {error_code}). It has not been placed on your account.",
        )

    # Logged only once Betfair has actually accepted the order, so betting.db never
    # records a 'confirmed' bet that never went on.
    log.info("Bet placed for slip %s on market %s", slip_id, betslip["marketId"])
    logger.log_bet_confirmed(slip_id=slip_id, time_to_confirm_ms=time_to_confirm_ms)

    return result


@router.post("/api/feedback", dependencies=[Depends(_require_session)])
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


@router.post("/api/search/feedback", dependencies=[Depends(_require_session)])
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
