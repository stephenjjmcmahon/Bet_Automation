import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, ANY
from fastapi.testclient import TestClient

from backend.main import app
from backend.api.routes import _require_session
from backend.schemas.bets import ClarificationResponse, ParsedBet
from backend.services import pending_slips
from backend.services.odds_service import MarketSuspendedError, InsufficientLiquidityError

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

# AIInterpreter.interpret returns a ClarificationResponse wrapping the parsed bet,
# not a bare ParsedBet.
INTERPRETED = ClarificationResponse(status="ok", parsed_bet=PARSED_BET)

CANDIDATES = [
    {"event": {"id": "35579868", "name": "Arsenal v Burnley", "openDate": "2026-05-18T15:00:00Z"}},
    {"event": {"id": "35580001", "name": "Arsenal v Anderlecht", "openDate": "2026-05-22T19:45:00Z"}},
]

EVENT_ID = "35579868"

MARKET_TYPES = ["MATCH_ODDS", "OVER_UNDER_25"]

# select_top_events returns {event_id, market_type} pairs, not bare event ids.
SELECTIONS = [{"event_id": EVENT_ID, "market_type": "MATCH_ODDS"}]

MARKET_IDS = {
    "eventId": "35579868",
    "marketId": "1.257879109",
    "selectionId": "1096",
}

LIVE_PRICE = 1.1

# Betfair returns HTTP 200 even for rejected bets, so a "success" fixture needs a
# SUCCESS status on both the report and every instruction report.
PLACED_OK = {"status": "SUCCESS", "instructionReports": [{"status": "SUCCESS"}]}
PLACE_REJECTED = {
    "status": "FAILURE",
    "instructionReports": [{"status": "FAILURE", "errorCode": "INVALID_BET_SIZE"}],
}


@pytest.fixture
def client():
    # Override session auth so tests don't need a real Betfair token in the session.
    app.dependency_overrides[_require_session] = lambda: None
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def _prepare(client, user_input="Back Arsenal vs Burnley 10"):
    """Call /api/prepare with all external calls mocked. Returns the response."""
    with patch("backend.api.routes.AIInterpreter.interpret", return_value=INTERPRETED), \
         patch("backend.api.routes.find_event_candidates", return_value=CANDIDATES), \
         patch("backend.api.routes.get_market_types", return_value=MARKET_TYPES), \
         patch("backend.api.routes.AIInterpreter.select_top_events", return_value=SELECTIONS), \
         patch("backend.api.routes.resolve_market", return_value=MARKET_IDS), \
         patch("backend.api.routes.get_best_price", return_value=LIVE_PRICE):
        return client.post("/api/prepare", json={"user_input": user_input})


# --- /api/prepare ---

class TestPrepare:
    def test_returns_200(self, client):
        assert _prepare(client).status_code == 200

    def test_slip_contains_live_price(self, client):
        assert _prepare(client).json()[0]["price"] == LIVE_PRICE

    def test_projected_return_uses_live_price(self, client):
        data = _prepare(client).json()[0]
        assert data["projected_return"] == round(10.0 * LIVE_PRICE, 2)

    def test_requested_price_is_none_when_not_stated(self, client):
        assert _prepare(client).json()[0]["requested_price"] is None

    def test_slip_fields_are_correct(self, client):
        data = _prepare(client).json()[0]
        assert data["selection_name"] == "Arsenal"
        assert data["stake"] == 10.0
        assert data["market_id"] == "1.257879109"
        assert data["side"] == "BACK"

    def test_returns_a_slip_id(self, client):
        data = _prepare(client).json()[0]
        assert "slip_id" in data
        assert len(data["slip_id"]) > 0

    def test_slip_is_stored_in_pending(self, client):
        # Verify the slip was stored by confirming it successfully.
        slip_id = _prepare(client).json()[0]["slip_id"]
        with patch("backend.api.routes.place_orders", return_value=PLACED_OK):
            assert client.post(f"/api/confirm/{slip_id}").status_code == 200

    def test_no_matching_event_returns_404(self, client):
        with patch("backend.api.routes.AIInterpreter.interpret", return_value=INTERPRETED), \
             patch("backend.api.routes.find_event_candidates", return_value=CANDIDATES), \
             patch("backend.api.routes.get_market_types", return_value=MARKET_TYPES), \
             patch("backend.api.routes.AIInterpreter.select_top_events", return_value=[]):
            response = client.post("/api/prepare", json={"user_input": "Back Arsenal 10"})
        assert response.status_code == 404

    def test_no_candidates_returns_404(self, client):
        with patch("backend.api.routes.AIInterpreter.interpret", return_value=INTERPRETED), \
             patch("backend.api.routes.find_event_candidates", return_value=[]), \
             patch("backend.api.routes.get_market_types", return_value=MARKET_TYPES), \
             patch("backend.api.routes.AIInterpreter.select_top_events", return_value=[]):
            response = client.post("/api/prepare", json={"user_input": "Back Arsenal 10"})
        assert response.status_code == 404

    def test_all_markets_suspended_returns_404(self, client):
        with patch("backend.api.routes.AIInterpreter.interpret", return_value=INTERPRETED), \
             patch("backend.api.routes.find_event_candidates", return_value=CANDIDATES), \
             patch("backend.api.routes.get_market_types", return_value=MARKET_TYPES), \
             patch("backend.api.routes.AIInterpreter.select_top_events", return_value=SELECTIONS), \
             patch("backend.api.routes.resolve_market", return_value=MARKET_IDS), \
             patch("backend.api.routes.get_best_price", side_effect=MarketSuspendedError("suspended")):
            response = client.post("/api/prepare", json={"user_input": "Back Arsenal 10"})
        assert response.status_code == 404

    def test_all_markets_low_liquidity_returns_404(self, client):
        with patch("backend.api.routes.AIInterpreter.interpret", return_value=INTERPRETED), \
             patch("backend.api.routes.find_event_candidates", return_value=CANDIDATES), \
             patch("backend.api.routes.get_market_types", return_value=MARKET_TYPES), \
             patch("backend.api.routes.AIInterpreter.select_top_events", return_value=SELECTIONS), \
             patch("backend.api.routes.resolve_market", return_value=MARKET_IDS), \
             patch("backend.api.routes.get_best_price", side_effect=InsufficientLiquidityError("low")):
            response = client.post("/api/prepare", json={"user_input": "Back Arsenal 10"})
        assert response.status_code == 404

    def test_no_slip_stored_on_failure(self, client):
        with patch("backend.api.routes.AIInterpreter.interpret", return_value=INTERPRETED), \
             patch("backend.api.routes.find_event_candidates", return_value=CANDIDATES), \
             patch("backend.api.routes.get_market_types", return_value=MARKET_TYPES), \
             patch("backend.api.routes.AIInterpreter.select_top_events", return_value=[]):
            client.post("/api/prepare", json={"user_input": "Back Arsenal 10"})
        # No slip was stored, so any confirm should 404.
        assert client.post("/api/confirm/any-id").status_code == 404


# --- /api/confirm ---

class TestConfirm:
    def test_confirm_calls_place_orders_with_correct_market(self, client):
        slip_id = _prepare(client).json()[0]["slip_id"]
        with patch("backend.api.routes.place_orders", return_value=PLACED_OK) as mock_place:
            client.post(f"/api/confirm/{slip_id}")
        mock_place.assert_called_once_with("1.257879109", ANY, ANY)

    def test_confirm_returns_betfair_response(self, client):
        slip_id = _prepare(client).json()[0]["slip_id"]
        with patch("backend.api.routes.place_orders", return_value=PLACED_OK):
            response = client.post(f"/api/confirm/{slip_id}")
        assert response.status_code == 200
        assert response.json() == PLACED_OK

    def test_confirm_removes_slip(self, client):
        slip_id = _prepare(client).json()[0]["slip_id"]
        with patch("backend.api.routes.place_orders", return_value=PLACED_OK):
            client.post(f"/api/confirm/{slip_id}")
        # Slip was removed — second confirm should 404.
        assert client.post(f"/api/confirm/{slip_id}").status_code == 404

    def test_double_confirm_returns_404(self, client):
        slip_id = _prepare(client).json()[0]["slip_id"]
        with patch("backend.api.routes.place_orders", return_value=PLACED_OK):
            client.post(f"/api/confirm/{slip_id}")
            second = client.post(f"/api/confirm/{slip_id}")
        assert second.status_code == 404

    def test_unknown_slip_id_returns_404(self, client):
        assert client.post("/api/confirm/not-a-real-id").status_code == 404

    def test_expired_slip_returns_404(self, client):
        slip_id = _prepare(client).json()[0]["slip_id"]
        expired_time = datetime.now(timezone.utc) - timedelta(seconds=pending_slips.SLIP_TTL_SECONDS + 1)
        with patch("backend.api.routes.pending_slips.pop", return_value=None), \
             patch("backend.api.routes.pending_slips.get_created_at", return_value=expired_time):
            assert client.post(f"/api/confirm/{slip_id}").status_code == 404

    def test_rejected_bet_returns_400(self, client):
        slip_id = _prepare(client).json()[0]["slip_id"]
        with patch("backend.api.routes.place_orders", return_value=PLACE_REJECTED):
            response = client.post(f"/api/confirm/{slip_id}")
        assert response.status_code == 400
        assert "INVALID_BET_SIZE" in response.json()["detail"]


# --- stake re-validation on confirm ---

class TestConfirmStakeRevalidation:
    """The frontend can edit the stake between prepare and confirm, so the amount
    that reaches Betfair may not be the one the prepare-time liquidity gate saw."""

    def test_larger_stake_without_liquidity_returns_409(self, client):
        slip_id = _prepare(client).json()[0]["slip_id"]
        with patch("backend.api.routes.get_best_price",
                   side_effect=InsufficientLiquidityError("not enough")), \
             patch("backend.api.routes.place_orders") as mock_place:
            response = client.post(f"/api/confirm/{slip_id}", json={"stake": 90.0})
        assert response.status_code == 409
        mock_place.assert_not_called()

    def test_larger_stake_on_suspended_market_returns_409(self, client):
        slip_id = _prepare(client).json()[0]["slip_id"]
        with patch("backend.api.routes.get_best_price",
                   side_effect=MarketSuspendedError("suspended")), \
             patch("backend.api.routes.place_orders") as mock_place:
            response = client.post(f"/api/confirm/{slip_id}", json={"stake": 90.0})
        assert response.status_code == 409
        mock_place.assert_not_called()

    def test_supported_larger_stake_is_placed_at_refreshed_price(self, client):
        slip_id = _prepare(client).json()[0]["slip_id"]
        refreshed_price = 1.35
        with patch("backend.api.routes.get_best_price", return_value=refreshed_price), \
             patch("backend.api.routes.place_orders", return_value=PLACED_OK) as mock_place:
            response = client.post(f"/api/confirm/{slip_id}", json={"stake": 25.0})
        assert response.status_code == 200
        limit_order = mock_place.call_args[0][1][0]["limitOrder"]
        assert limit_order["size"] == 25.0
        # Stale prepare-time price must not survive a re-price.
        assert limit_order["price"] == refreshed_price

    def test_unchanged_stake_does_not_refetch_the_book(self, client):
        slip_id = _prepare(client).json()[0]["slip_id"]
        with patch("backend.api.routes.get_best_price") as mock_price, \
             patch("backend.api.routes.place_orders", return_value=PLACED_OK):
            response = client.post(f"/api/confirm/{slip_id}", json={"stake": 10.0})
        assert response.status_code == 200
        mock_price.assert_not_called()

    @pytest.mark.parametrize("bad_stake", [-5, 0, 100_000])
    def test_out_of_bounds_stake_returns_422(self, client, bad_stake):
        slip_id = _prepare(client).json()[0]["slip_id"]
        with patch("backend.api.routes.place_orders") as mock_place:
            response = client.post(f"/api/confirm/{slip_id}", json={"stake": bad_stake})
        assert response.status_code == 422
        mock_place.assert_not_called()


# --- bet logging ---

class TestConfirmLogging:
    def test_logs_confirmed_bet_once_placed(self, client, mock_logger):
        slip_id = _prepare(client).json()[0]["slip_id"]
        with patch("backend.api.routes.place_orders", return_value=PLACED_OK):
            client.post(f"/api/confirm/{slip_id}")
        mock_logger["log_bet_confirmed"].assert_called_once()

    def test_does_not_log_when_betfair_rejects_the_bet(self, client, mock_logger):
        """Otherwise betting.db records a 'confirmed' bet that never went on."""
        slip_id = _prepare(client).json()[0]["slip_id"]
        with patch("backend.api.routes.place_orders", return_value=PLACE_REJECTED):
            response = client.post(f"/api/confirm/{slip_id}")
        assert response.status_code == 400
        mock_logger["log_bet_confirmed"].assert_not_called()
