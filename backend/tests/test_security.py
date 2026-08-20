"""Covers the Phase 2 hardening: endpoint auth, CORS, error shape, startup
validation and rate limiting."""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.api.rate_limit import limiter
from backend.api.routes import _require_session
from backend.main import app, validate_environment
from backend.services.betfair_auth import SessionExpiredError


@pytest.fixture
def anon_client():
    """No session override — requests arrive unauthenticated, as an attacker's would."""
    app.dependency_overrides.clear()
    return TestClient(app)


@pytest.fixture
def client():
    app.dependency_overrides[_require_session] = lambda: None
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


# --- 2.4 auth on previously unprotected endpoints ---

@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/feedback", {"input": "x", "output": {}, "correct": True}),
        ("/api/search/feedback", {"search_id": 1, "correct": True}),
        ("/api/fixtures", {"team_name": "Arsenal", "sport": "football"}),
    ],
)
def test_endpoint_requires_a_session(anon_client, path, body):
    assert anon_client.post(path, json=body).status_code == 401


def test_search_reset_stays_unauthenticated(anon_client):
    """Deliberate: the 'New search' button must work after the Betfair token expires."""
    assert anon_client.post("/api/search/reset").status_code == 200


# --- 2.5 session expiry must surface as 401, not 500 ---

def test_fixtures_session_expiry_returns_401(client):
    with patch("backend.api.routes.get_upcoming_fixtures", side_effect=SessionExpiredError("expired")):
        response = client.post("/api/fixtures", json={"team_name": "Arsenal", "sport": "football"})
    assert response.status_code == 401


def test_fixtures_does_not_leak_internal_error_text(client):
    with patch("backend.api.routes.get_upcoming_fixtures", side_effect=RuntimeError("secret internals")):
        response = client.post("/api/fixtures", json={"team_name": "Arsenal", "sport": "football"})
    assert response.status_code == 500
    assert "secret internals" not in response.json()["detail"]


def test_login_does_not_leak_internal_error_text(anon_client):
    with patch("backend.api.routes.login", side_effect=RuntimeError("connection to 10.0.0.5 refused")):
        response = anon_client.post("/api/login", json={"username": "u", "password": "p"})
    assert response.status_code == 502
    assert "10.0.0.5" not in response.json()["detail"]


# --- 2.1 CORS ---

def test_cors_is_not_a_wildcard():
    assert "*" not in main.ALLOWED_ORIGINS


def test_cors_rejects_an_unknown_origin(anon_client):
    response = anon_client.get("/health", headers={"Origin": "https://evil.example"})
    assert response.headers.get("access-control-allow-origin") != "https://evil.example"


def test_cors_allows_the_configured_origin(anon_client):
    origin = main.ALLOWED_ORIGINS[0]
    response = anon_client.get("/health", headers={"Origin": origin})
    assert response.headers.get("access-control-allow-origin") == origin


# --- 5.3 startup validation + health ---

def test_health_returns_ok(anon_client):
    assert anon_client.get("/health").json() == {"status": "ok"}


def test_validate_environment_raises_on_missing_keys():
    with patch.dict(os.environ, {"BETFAIR_APP_KEY": "", "OPENAI_API_KEY": ""}, clear=False):
        with pytest.raises(RuntimeError, match="BETFAIR_APP_KEY"):
            validate_environment()


def test_validate_environment_rejects_the_default_secret_in_production():
    env = {
        "BETFAIR_APP_KEY": "k",
        "OPENAI_API_KEY": "k",
        "SESSION_SECRET_KEY": main.DEV_SESSION_SECRET,
    }
    with patch.dict(os.environ, env, clear=False), patch.object(main, "IS_PRODUCTION", True):
        with pytest.raises(RuntimeError, match="SESSION_SECRET_KEY"):
            validate_environment()


def test_validate_environment_passes_with_a_real_secret_in_production():
    env = {
        "BETFAIR_APP_KEY": "k",
        "OPENAI_API_KEY": "k",
        "SESSION_SECRET_KEY": "a-real-private-value",
    }
    with patch.dict(os.environ, env, clear=False), patch.object(main, "IS_PRODUCTION", True):
        validate_environment()  # must not raise


# --- 2.6 rate limiting ---

class TestRateLimit:
    @pytest.fixture(autouse=True)
    def enable_limiter(self):
        """conftest disables the limiter globally; turn it back on just for these."""
        limiter.enabled = True
        limiter.reset()
        yield
        limiter.reset()
        limiter.enabled = False

    def test_login_is_rate_limited(self, anon_client):
        with patch("backend.api.routes.login", side_effect=ValueError("bad credentials")):
            statuses = [
                anon_client.post("/api/login", json={"username": "u", "password": "p"}).status_code
                for _ in range(15)
            ]
        assert 429 in statuses

    def test_query_is_rate_limited(self, client):
        with patch("backend.api.routes.classify_intent", return_value="bet"), \
             patch("backend.api.routes._prepare_slips", return_value=[]):
            statuses = [
                client.post("/api/query", json={"user_input": "back arsenal 10"}).status_code
                for _ in range(40)
            ]
        assert 429 in statuses
