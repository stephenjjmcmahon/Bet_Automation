from unittest.mock import patch

import pytest

from backend.services.odds_service import (
    InsufficientLiquidityError,
    MarketSuspendedError,
    get_best_price,
)

MARKET_ID = "1.234567"
SELECTION_ID = "987654"


def _book(status="OPEN", runner_status="ACTIVE", back_offers=None, lay_offers=None):
    """Build a minimal market book dict for testing."""
    return {
        "marketId": MARKET_ID,
        "status": status,
        "runners": [
            {
                "selectionId": 987654,  # int, as Betfair returns it
                "status": runner_status,
                "ex": {
                    "availableToBack": back_offers if back_offers is not None else [
                        {"price": 5.2, "size": 200.0},
                        {"price": 5.0, "size": 400.0},
                    ],
                    "availableToLay": lay_offers if lay_offers is not None else [
                        {"price": 5.4, "size": 80.0},
                    ],
                },
            }
        ],
    }


# --- BACK bets ---

def test_back_returns_best_back_price():
    with patch("backend.services.odds_service.get_market_book", return_value=_book()):
        price = get_best_price(MARKET_ID, SELECTION_ID, "BACK", stake=50.0, session={})
    assert price == 5.2


def test_back_uses_top_of_book_not_second():
    book = _book(back_offers=[{"price": 6.0, "size": 500.0}, {"price": 5.8, "size": 300.0}])
    with patch("backend.services.odds_service.get_market_book", return_value=book):
        price = get_best_price(MARKET_ID, SELECTION_ID, "BACK", stake=50.0, session={})
    assert price == 6.0


# --- LAY bets ---

def test_lay_returns_best_lay_price():
    with patch("backend.services.odds_service.get_market_book", return_value=_book()):
        price = get_best_price(MARKET_ID, SELECTION_ID, "LAY", stake=50.0, session={})
    assert price == 5.4


# --- Market status ---

def test_suspended_market_raises():
    with patch("backend.services.odds_service.get_market_book", return_value=_book(status="SUSPENDED")):
        with pytest.raises(MarketSuspendedError):
            get_best_price(MARKET_ID, SELECTION_ID, "BACK", stake=50.0, session={})


# --- Runner status ---

def test_unknown_selection_raises():
    with patch("backend.services.odds_service.get_market_book", return_value=_book()):
        with pytest.raises(ValueError, match="not found"):
            get_best_price(MARKET_ID, "999999", "BACK", stake=50.0, session={})


def test_inactive_runner_raises():
    with patch("backend.services.odds_service.get_market_book", return_value=_book(runner_status="REMOVED")):
        with pytest.raises(ValueError, match="not active"):
            get_best_price(MARKET_ID, SELECTION_ID, "BACK", stake=50.0, session={})


# --- Liquidity ---

def test_no_back_offers_raises():
    with patch("backend.services.odds_service.get_market_book", return_value=_book(back_offers=[])):
        with pytest.raises(InsufficientLiquidityError):
            get_best_price(MARKET_ID, SELECTION_ID, "BACK", stake=50.0, session={})


def test_no_lay_offers_raises():
    with patch("backend.services.odds_service.get_market_book", return_value=_book(lay_offers=[])):
        with pytest.raises(InsufficientLiquidityError):
            get_best_price(MARKET_ID, SELECTION_ID, "LAY", stake=50.0, session={})


def test_stake_exceeds_available_size_raises():
    book = _book(back_offers=[{"price": 5.2, "size": 30.0}])
    with patch("backend.services.odds_service.get_market_book", return_value=book):
        with pytest.raises(InsufficientLiquidityError, match="Insufficient liquidity"):
            get_best_price(MARKET_ID, SELECTION_ID, "BACK", stake=100.0, session={})


def test_stake_exactly_equal_to_available_size_passes():
    book = _book(back_offers=[{"price": 5.2, "size": 100.0}])
    with patch("backend.services.odds_service.get_market_book", return_value=book):
        price = get_best_price(MARKET_ID, SELECTION_ID, "BACK", stake=100.0, session={})
    assert price == 5.2


# --- Selection ID type tolerance ---

def test_selection_id_matched_as_string():
    """Betfair returns selectionId as int; we pass it as str. Should still match."""
    with patch("backend.services.odds_service.get_market_book", return_value=_book()):
        price = get_best_price(MARKET_ID, "987654", "BACK", stake=50.0, session={})
    assert price == 5.2
