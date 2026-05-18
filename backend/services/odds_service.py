"""
odds_service.py
---------------
Extracts the best available price for a specific runner from a Betfair market book
and validates that enough liquidity exists to cover the requested stake.

This sits between betfair_client.get_market_book() (raw API call) and the
/api/prepare route (business logic). It owns all the rules about what counts
as a valid, tradeable price.

Betfair price ladder
--------------------
Betfair uses a fixed set of valid prices (the "price ladder"). Prices cannot be
arbitrary floats — they increment in steps that get wider as the price increases:

    1.01 – 2.00  : steps of 0.01  (e.g. 1.50, 1.51, 1.52 ...)
    2.00 – 3.00  : steps of 0.02  (e.g. 2.00, 2.02, 2.04 ...)
    3.00 – 4.00  : steps of 0.05  (e.g. 3.00, 3.05, 3.10 ...)
    4.00 – 6.00  : steps of 0.1   (e.g. 4.0, 4.1, 4.2 ...)
    6.00 – 10.0  : steps of 0.2
    10.0 – 20.0  : steps of 0.5
    20.0 – 30.0  : steps of 1.0
    30.0 – 50.0  : steps of 2.0
    50.0 – 100.0 : steps of 5.0
    100.0 – 1000 : steps of 10.0

The prices returned by listMarketBook are always valid ladder prices, so we
use them directly without rounding.

Back vs Lay
-----------
For a BACK bet  : we want availableToBack[0]["price"]
                  This is the highest price someone is currently offering to lay,
                  so it's the best price a backer can immediately get matched at.

For a LAY bet   : we want availableToLay[0]["price"]
                  This is the lowest price someone wants to back at,
                  so it's the best price a layer can be immediately matched at.

Liquidity check
---------------
availableToBack[0]["size"] is how much total stake is available AT that price
(after rollup aggregation set in the priceProjection). We require this to be
>= the user's stake so the bet can be fully matched immediately. If it isn't,
the bet would be partially matched or sit unmatched in the market.
"""

from backend.services.betfair_client import get_market_book


class MarketSuspendedError(Exception):
    """Raised when the target market is currently suspended."""


class InsufficientLiquidityError(Exception):
    """Raised when available size at the best price is less than the requested stake."""


def get_best_price(market_id: str, selection_id: str, side: str, stake: float) -> float:
    """
    Return the best available live price for a runner and validate liquidity.

    Fetches a fresh market book from Betfair, locates the requested runner,
    and returns the top-of-book price for the requested side. Also checks that
    enough liquidity exists at that price to cover the full stake.

    Parameters
    ----------
    market_id : str
        Betfair market ID, e.g. "1.234567".
    selection_id : str
        Betfair selection (runner) ID, e.g. "987654". Compared as string to
        avoid int/str mismatch from the API response.
    side : str
        "BACK" or "LAY".
    stake : float
        The stake the user wants to place, in GBP. Used for liquidity check.

    Returns
    -------
    float
        Best available price at the top of the order book for the given side.

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
    book = get_market_book(market_id)

    if book["status"] == "SUSPENDED":
        raise MarketSuspendedError(
            f"Market {market_id} is suspended — prices are unavailable until it reopens."
        )

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
