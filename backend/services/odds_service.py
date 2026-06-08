
from backend.services.betfair_client import get_market_book


class MarketSuspendedError(Exception):
    """Raised when the target market is currently suspended."""


class InsufficientLiquidityError(Exception):
    """Raised when available size at the best price is less than the requested stake."""


def get_best_price(market_id: str, selection_id: str, side: str, stake: float, session: dict, line: float | None = None) -> float:
    """
    Return the best available live price for a runner and validate liquidity.

    Fetches a fresh market book from Betfair, locates the requested runner,
    and returns the top-of-book price for the requested side. Also checks that
    enough liquidity exists at that price to cover the full stake.

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
    book = get_market_book(market_id, session)

    if book["status"] == "SUSPENDED":
        raise MarketSuspendedError(
            f"Market {market_id} is suspended — prices are unavailable until it reopens."
        )

    if line is not None:
        matching_runners = [r for r in book["runners"] if str(r["selectionId"]) == str(selection_id)]
        print(f"  DEBUG get_best_price — selectionId={selection_id} line={line}")
        print(f"  DEBUG book runners for that selectionId (all {len(matching_runners)}): {[(r.get('handicap'), r.get('status')) for r in matching_runners]}")
        print()
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

    if available_size < stake:
        raise InsufficientLiquidityError(
            f"Insufficient liquidity for {side} bet on runner {selection_id}. "
            f"Requested stake: £{stake}, available at best price ({best_price}): £{available_size}."
        )

    return best_price
