"""Tests for POST /api/prepare-from-market — building a slip from a market the
user picked in search results, reusing the shared _persist_slip helper.

Calls the route function directly with a fake request (session is a plain dict,
which is all pending_slips needs); get_best_price is mocked so no Betfair call.
"""
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backend.api import routes
from backend.api.routes import PrepareFromMarketRequest, prepare_from_market
from backend.services.odds_service import InsufficientLiquidityError, MarketSuspendedError


class FakeRequest:
    def __init__(self):
        self.session = {}


def _body(**kw):
    base = {
        "event_id": "e1", "market_id": "1.23", "selection_id": 47, "side": "BACK",
        "stake": 10.0, "runner_name": "Over 2.5", "event_name": "A v B",
        "market_type": "OVER_UNDER_25",
    }
    base.update(kw)
    return PrepareFromMarketRequest(**base)


def test_builds_and_stores_slip():
    req = FakeRequest()
    with patch.object(routes, "get_best_price", return_value=1.95):
        slip = prepare_from_market(req, _body())

    assert slip.price == 1.95
    assert slip.selection_id == 47
    assert slip.market_id == "1.23"
    assert slip.stake == 10.0
    assert slip.projected_return == round(10.0 * 1.95, 2)
    # Confirmable: the slip is in the session pending store under its id.
    assert slip.slip_id in req.session.get("pending_slips", {})


def test_insufficient_liquidity_returns_409():
    req = FakeRequest()
    with patch.object(routes, "get_best_price", side_effect=InsufficientLiquidityError("thin")):
        with pytest.raises(HTTPException) as ei:
            prepare_from_market(req, _body())
    assert ei.value.status_code == 409


def test_suspended_market_returns_409():
    req = FakeRequest()
    with patch.object(routes, "get_best_price", side_effect=MarketSuspendedError("suspended")):
        with pytest.raises(HTTPException) as ei:
            prepare_from_market(req, _body())
    assert ei.value.status_code == 409


def test_runner_gone_returns_400():
    req = FakeRequest()
    with patch.object(routes, "get_best_price", side_effect=ValueError("not found")):
        with pytest.raises(HTTPException) as ei:
            prepare_from_market(req, _body())
    assert ei.value.status_code == 400
