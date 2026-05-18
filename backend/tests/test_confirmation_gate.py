import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, ANY
from fastapi.testclient import TestClient

from backend.main import app
from backend.schemas.bets import ParsedBet
from backend.services import pending_slips
from backend.services.odds_service import MarketSuspendedError, InsufficientLiquidityError

client = TestClient(app)

PARSED_BET = ParsedBet(
    selection_name="Arsenal",
    sport="soccer",
    side="BACK",
    stake=10.0,
    price=None,
    market_type="MATCH_ODDS",
    opponent="Burnley",
    competition="Premier League",
    match_date=None,
)

CANDIDATES = [
    {"event": {"id": "35579868", "name": "Arsenal v Burnley", "openDate": "2026-05-18T15:00:00Z"}},
    {"event": {"id": "35580001", "name": "Arsenal v Anderlecht", "openDate": "2026-05-22T19:45:00Z"}},
]

EVENT_ID = "35579868"

MARKET_IDS = {
    "eventId": "35579868",
    "marketId": "1.257879109",
    "selectionId": "1096",
}

LIVE_PRICE = 1.1


@pytest.fixture(autouse=True)
def clear_store():
    pending_slips._store.clear()
    yield
    pending_slips._store.clear()


def _prepare(user_input="Back Arsenal vs Burnley 10"):
    """Call /api/prepare with all external calls mocked. Returns the response."""
    with patch("backend.api.routes.AIInterpreter.interpret", return_value=PARSED_BET), \
         patch("backend.api.routes.find_event_candidates", return_value=CANDIDATES), \
         patch("backend.api.routes.AIInterpreter.select_event", return_value=EVENT_ID), \
         patch("backend.api.routes.resolve_market", return_value=MARKET_IDS), \
         patch("backend.api.routes.get_best_price", return_value=LIVE_PRICE):
        return client.post("/api/prepare", json={"user_input": user_input})


# --- /api/prepare ---

class TestPrepare:
    def test_returns_200(self):
        assert _prepare().status_code == 200

    def test_slip_contains_live_price(self):
        assert _prepare().json()["price"] == LIVE_PRICE

    def test_projected_return_uses_live_price(self):
        data = _prepare().json()
        assert data["projected_return"] == round(10.0 * LIVE_PRICE, 2)

    def test_requested_price_is_none_when_not_stated(self):
        assert _prepare().json()["requested_price"] is None

    def test_slip_fields_are_correct(self):
        data = _prepare().json()
        assert data["selection_name"] == "Arsenal"
        assert data["stake"] == 10.0
        assert data["market_id"] == "1.257879109"
        assert data["side"] == "BACK"

    def test_returns_a_slip_id(self):
        data = _prepare().json()
        assert "slip_id" in data
        assert len(data["slip_id"]) > 0

    def test_slip_is_stored_in_pending(self):
        slip_id = _prepare().json()["slip_id"]
        assert slip_id in pending_slips._store

    def test_no_matching_event_returns_404(self):
        with patch("backend.api.routes.AIInterpreter.interpret", return_value=PARSED_BET), \
             patch("backend.api.routes.find_event_candidates", return_value=CANDIDATES), \
             patch("backend.api.routes.AIInterpreter.select_event", return_value=None):
            response = client.post("/api/prepare", json={"user_input": "Back Arsenal 10"})
        assert response.status_code == 404

    def test_no_candidates_returns_404(self):
        with patch("backend.api.routes.AIInterpreter.interpret", return_value=PARSED_BET), \
             patch("backend.api.routes.find_event_candidates", return_value=[]), \
             patch("backend.api.routes.AIInterpreter.select_event", return_value=None):
            response = client.post("/api/prepare", json={"user_input": "Back Arsenal 10"})
        assert response.status_code == 404

    def test_suspended_market_returns_503(self):
        with patch("backend.api.routes.AIInterpreter.interpret", return_value=PARSED_BET), \
             patch("backend.api.routes.find_event_candidates", return_value=CANDIDATES), \
             patch("backend.api.routes.AIInterpreter.select_event", return_value=EVENT_ID), \
             patch("backend.api.routes.resolve_market", return_value=MARKET_IDS), \
             patch("backend.api.routes.get_best_price", side_effect=MarketSuspendedError("suspended")):
            response = client.post("/api/prepare", json={"user_input": "Back Arsenal 10"})
        assert response.status_code == 503

    def test_insufficient_liquidity_returns_409(self):
        with patch("backend.api.routes.AIInterpreter.interpret", return_value=PARSED_BET), \
             patch("backend.api.routes.find_event_candidates", return_value=CANDIDATES), \
             patch("backend.api.routes.AIInterpreter.select_event", return_value=EVENT_ID), \
             patch("backend.api.routes.resolve_market", return_value=MARKET_IDS), \
             patch("backend.api.routes.get_best_price", side_effect=InsufficientLiquidityError("low")):
            response = client.post("/api/prepare", json={"user_input": "Back Arsenal 10"})
        assert response.status_code == 409

    def test_no_slip_stored_on_failure(self):
        with patch("backend.api.routes.AIInterpreter.interpret", return_value=PARSED_BET), \
             patch("backend.api.routes.find_event_candidates", return_value=CANDIDATES), \
             patch("backend.api.routes.AIInterpreter.select_event", return_value=None):
            client.post("/api/prepare", json={"user_input": "Back Arsenal 10"})
        assert len(pending_slips._store) == 0


# --- /api/confirm ---

class TestConfirm:
    def test_confirm_calls_place_orders_with_correct_market(self):
        slip_id = _prepare().json()["slip_id"]
        with patch("backend.api.routes.place_orders", return_value={"status": "SUCCESS"}) as mock_place:
            client.post(f"/api/confirm/{slip_id}")
        mock_place.assert_called_once_with("1.257879109", ANY)

    def test_confirm_returns_betfair_response(self):
        slip_id = _prepare().json()["slip_id"]
        betfair_response = {"status": "SUCCESS", "instructionReports": []}
        with patch("backend.api.routes.place_orders", return_value=betfair_response):
            response = client.post(f"/api/confirm/{slip_id}")
        assert response.status_code == 200
        assert response.json() == betfair_response

    def test_confirm_removes_slip(self):
        slip_id = _prepare().json()["slip_id"]
        with patch("backend.api.routes.place_orders", return_value={}):
            client.post(f"/api/confirm/{slip_id}")
        assert slip_id not in pending_slips._store

    def test_double_confirm_returns_404(self):
        slip_id = _prepare().json()["slip_id"]
        with patch("backend.api.routes.place_orders", return_value={}):
            client.post(f"/api/confirm/{slip_id}")
            second = client.post(f"/api/confirm/{slip_id}")
        assert second.status_code == 404

    def test_unknown_slip_id_returns_404(self):
        assert client.post("/api/confirm/not-a-real-id").status_code == 404

    def test_expired_slip_returns_404(self):
        slip_id = _prepare().json()["slip_id"]
        pending_slips._store[slip_id]["created_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=pending_slips.SLIP_TTL_SECONDS + 1)
        )
        assert client.post(f"/api/confirm/{slip_id}").status_code == 404
