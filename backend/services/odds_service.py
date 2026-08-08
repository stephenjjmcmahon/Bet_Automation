import logging

from backend.services.betfair_client import get_market_book

log = logging.getLogger(__name__)


class MarketSuspendedError(Exception):
    """Raised when the target market is currently suspended."""


class InsufficientLiquidityError(Exception):
    """Raised when available size at the best price is less than the requested stake."""


def get_best_price(market_id: str, selection_id: str, side: str, stake: float, session: dict, line: float | None = None, book: dict | None = None) -> float:
    """
    Return the best available live price for a runner and validate liquidity.

    Locates the requested runner in the market book and returns the top-of-book
    price for the requested side, checking that enough liquidity exists at that
    price to cover the full stake. `book` may be a market book already fetched by
    the resolver (which fetches one to drop inactive runners) so the same request
    isn't made to Betfair twice; when omitted a fresh book is fetched here.

    Raises
    ------
    MarketSuspendedError
        If the market status is SUSPENDED. Prices during suspension are
        unreliable and bets cannot be placed.
    ValueError
        If the runner is not found in the market, or the runner's status is
        not ACTIVE (e.g. REMOVED, WINNER, LOSER).
    InsufficientLiquidityError
        If there are no offers on the requested side, or the available size
        at the best price is smaller than the requested stake.
    """
    if book is None:
        book = get_market_book(market_id, session)

    if book["status"] == "SUSPENDED":
        raise MarketSuspendedError(
            f"Market {market_id} is suspended — prices are unavailable until it reopens."
        )

    if line is not None:
        matching_runners = [r for r in book["runners"] if str(r["selectionId"]) == str(selection_id)]
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "get_best_price selectionId=%s line=%s — %d row(s) in book: %s",
                selection_id, line, len(matching_runners),
                [(r.get("handicap"), r.get("status")) for r in matching_runners],
            )
        runner = next(
            (r for r in matching_runners if r.get("handicap") == line),
            None,
        )
    else:
        runner = next(
            (r for r in book["runners"] if str(r["selectionId"]) == str(selection_id)),
            None,
        )

    if runner is None:
        raise ValueError(
            f"Runner {selection_id} not found in market {market_id}. "
            "The selection may have been removed."
        )

    if runner["status"] != "ACTIVE":
        raise ValueError(
            f"Runner {selection_id} is not active (status: {runner['status']}). "
            "Cannot fetch a price for an inactive runner."
        )

    price_key = "availableToBack" if side == "BACK" else "availableToLay"
    offers = runner["ex"].get(price_key, [])

    if not offers:
        raise InsufficientLiquidityError(
            f"No {side} offers available for runner {selection_id} in market {market_id}."
        )

    best = offers[0]
    best_price = best["price"]
    available_size = best["size"]

    # 1.01 is Betfair's price floor. When backing, an offer at the floor means
    # nobody is pricing the selection for real (common on freshly-opened, thin
    # markets) — backing at 1.01 returns essentially the stake. Treat it as no
    # real market rather than presenting a misleading slip.
    if side == "BACK" and best_price <= 1.01:
        raise InsufficientLiquidityError(
            f"No real market price for runner {selection_id} in market {market_id} — "
            f"the only back offer is at the 1.01 floor."
        )

    if available_size < stake:
        raise InsufficientLiquidityError(
            f"Insufficient liquidity for {side} bet on runner {selection_id}. "
            f"Requested stake: £{stake}, available at best price ({best_price}): £{available_size}."
        )

    return best_price
