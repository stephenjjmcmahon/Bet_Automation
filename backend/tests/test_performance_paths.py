"""Tests for the performance work: pooled HTTP, the bulk-catalogue TTL cache,
the concurrent resolver fan-out, and the reused SQLite connection.

These paths are about *not* changing observable behaviour while doing less work,
so the assertions are mostly equivalence assertions: same ordering, same error
handling, same number of logical results — with fewer underlying calls. Several
of them also cover route branches the existing suite never reached (a
multi-candidate /api/prepare, and the racing branch end to end).
"""
import threading
import time
from copy import deepcopy
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.api.routes import _require_session
from backend.main import app
from backend.schemas.bets import ClarificationResponse, ParsedBet
from backend.services import betfair_client, racing_service
from backend.services.betfair_auth import SessionExpiredError
from backend.services.concurrency import parallel_map

# ── parallel_map ──────────────────────────────────────────────────────────────

class TestParallelMap:
    def test_empty_input(self):
        assert parallel_map(lambda x: x, []) == []

    def test_preserves_input_order(self):
        # Sleep inversely to position, so completion order is the reverse of input
        # order — if results came back as-completed this would fail.
        def slow(n):
            time.sleep((5 - n) * 0.01)
            return n

        results = parallel_map(slow, [1, 2, 3, 4])
        assert [r for r, _ in results] == [1, 2, 3, 4]
        assert all(e is None for _, e in results)

    def test_actually_runs_concurrently(self):
        seen = set()
        barrier = threading.Barrier(3, timeout=5)

        def f(n):
            seen.add(n)
            barrier.wait()   # only returns if all three run at once
            return n

        results = parallel_map(f, [1, 2, 3])
        assert [r for r, _ in results] == [1, 2, 3]
        assert seen == {1, 2, 3}

    def test_captures_exception_per_item_without_failing_the_rest(self):
        def f(n):
            if n == 2:
                raise ValueError("boom")
            return n * 10

        results = parallel_map(f, [1, 2, 3])
        assert results[0] == (10, None)
        assert results[2] == (30, None)
        assert results[1][0] is None
        assert isinstance(results[1][1], ValueError)

    def test_single_item_still_captures_exception(self):
        # The len == 1 fast path bypasses the executor — it must keep the same
        # (result, exception) contract.
        (result, exc), = parallel_map(lambda n: 1 / 0, [1])
        assert result is None
        assert isinstance(exc, ZeroDivisionError)


# ── bulk-catalogue TTL cache ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_cache():
    betfair_client.clear_catalogue_cache()
    yield
    betfair_client.clear_catalogue_cache()


class TestCatalogueCache:
    def test_second_call_within_ttl_hits_no_api(self):
        calls = []

        def fake_post(path, payload, session):
            calls.append(payload)
            return [{"marketId": "1.1", "runners": []}]

        with patch.object(betfair_client, "betfair_post", side_effect=fake_post):
            first = betfair_client.list_racing_markets("7", "WIN", {})
            second = betfair_client.list_racing_markets("7", "WIN", {})

        assert len(calls) == 1          # the repeat was served from cache
        assert first == second          # and returned identical data

    def test_different_market_types_are_cached_separately(self):
        calls = []

        def fake_post(path, payload, session):
            calls.append(payload["filter"]["marketTypeCodes"][0])
            return []

        with patch.object(betfair_client, "betfair_post", side_effect=fake_post):
            betfair_client.list_racing_markets("7", "WIN", {})
            betfair_client.list_racing_markets("7", "ANTEPOST_WIN", {})
            betfair_client.list_racing_markets("7", "WIN", {})

        assert calls == ["WIN", "ANTEPOST_WIN"]

    def test_different_sports_are_cached_separately(self):
        calls = []

        with patch.object(betfair_client, "betfair_post",
                          side_effect=lambda p, pay, s: calls.append(pay) or []):
            betfair_client.list_racing_markets("7", "WIN", {})       # horses
            betfair_client.list_racing_markets("4339", "WIN", {})    # greyhounds
        assert len(calls) == 2

    def test_entry_expires_after_ttl(self):
        calls = []
        with patch.object(betfair_client, "CATALOGUE_TTL_SECONDS", 0.05), \
             patch.object(betfair_client, "betfair_post",
                          side_effect=lambda p, pay, s: calls.append(1) or []):
            betfair_client.list_racing_markets("7", "WIN", {})
            time.sleep(0.08)
            betfair_client.list_racing_markets("7", "WIN", {})
        assert len(calls) == 2

    def test_ttl_zero_disables_the_cache(self):
        calls = []
        with patch.object(betfair_client, "CATALOGUE_TTL_SECONDS", 0), \
             patch.object(betfair_client, "betfair_post",
                          side_effect=lambda p, pay, s: calls.append(1) or []):
            betfair_client.list_racing_markets("7", "WIN", {})
            betfair_client.list_racing_markets("7", "WIN", {})
        assert len(calls) == 2

    def test_caller_mutating_the_returned_list_cannot_corrupt_the_cache(self):
        with patch.object(betfair_client, "betfair_post",
                          return_value=[{"marketId": "1.1"}]):
            first = betfair_client.list_racing_markets("7", "WIN", {})
        first.append({"marketId": "injected"})

        with patch.object(betfair_client, "betfair_post",
                          side_effect=AssertionError("should have been cached")):
            second = betfair_client.list_racing_markets("7", "WIN", {})
        assert second == [{"marketId": "1.1"}]

    def test_outright_markets_are_cached_too(self):
        calls = []
        with patch.object(betfair_client, "betfair_post",
                          side_effect=lambda p, pay, s: calls.append(1) or []):
            betfair_client.list_outright_markets("1", {})
            betfair_client.list_outright_markets("1", {})
        assert len(calls) == 1

    def test_racing_and_outright_pools_do_not_collide(self):
        calls = []
        with patch.object(betfair_client, "betfair_post",
                          side_effect=lambda p, pay, s: calls.append(1) or []):
            betfair_client.list_racing_markets("1", "WIN", {})
            betfair_client.list_outright_markets("1", {})
        assert len(calls) == 2


# ── concurrent resolution in /api/prepare ─────────────────────────────────────

PARSED_BET = ParsedBet(
    selection_name="Arsenal", sport="soccer", side="BACK", stake=10.0,
    market_type="MATCH_ODDS",
)
INTERPRETED = ClarificationResponse(status="ok", parsed_bet=PARSED_BET)

CANDIDATES = [
    {"event": {"id": "E1", "name": "Arsenal v Burnley", "openDate": "2026-05-18T15:00:00Z"}},
    {"event": {"id": "E2", "name": "Arsenal v Anderlecht", "openDate": "2026-05-22T19:45:00Z"}},
    {"event": {"id": "E3", "name": "Arsenal v Ajax", "openDate": "2026-05-25T19:45:00Z"}},
]
# Three ranked candidates — this is what makes the concurrent path run at all
# (the pre-existing suite only ever supplied one).
SELECTIONS = [
    {"event_id": "E1", "market_type": "MATCH_ODDS"},
    {"event_id": "E2", "market_type": "MATCH_ODDS"},
    {"event_id": "E3", "market_type": "MATCH_ODDS"},
]


def _market_ids(event_id):
    return {
        "eventId": event_id,
        "marketId": f"1.{event_id}",
        "selectionId": "1096",
        "book": {"status": "OPEN"},
    }


@pytest.fixture
def client():
    app.dependency_overrides[_require_session] = lambda: None
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def _prepare(client, resolve, price=2.0):
    with patch("backend.api.routes.AIInterpreter.interpret", return_value=INTERPRETED), \
         patch("backend.api.routes.find_event_candidates", return_value=CANDIDATES), \
         patch("backend.api.routes.get_market_types", return_value=["MATCH_ODDS"]), \
         patch("backend.api.routes.AIInterpreter.select_top_events", return_value=SELECTIONS), \
         patch("backend.api.routes.resolve_market", side_effect=resolve), \
         patch("backend.api.routes.get_best_price", return_value=price):
        return client.post("/api/prepare", json={"user_input": "Back Arsenal 10"})


class TestConcurrentCandidateResolution:
    def test_slips_stay_in_candidate_rank_order(self, client):
        # Slowest first: if slips were appended as resolutions completed rather
        # than in rank order, E1 would no longer be first.
        def resolve(event_id, *a, **kw):
            time.sleep({"E1": 0.06, "E2": 0.03, "E3": 0.0}[event_id])
            return _market_ids(event_id)

        body = _prepare(client, resolve).json()
        assert [s["event_id"] for s in body] == ["E1", "E2", "E3"]

    def test_every_slip_is_stored_and_confirmable(self, client):
        # The real hazard of concurrency here: pending_slips.save does a
        # read-modify-write of the session dict, so a slip could be lost.
        body = _prepare(client, lambda event_id, *a, **kw: _market_ids(event_id)).json()
        assert len({s["slip_id"] for s in body}) == 3

        placed = {"status": "SUCCESS", "instructionReports": [{"status": "SUCCESS"}]}
        with patch("backend.api.routes.place_orders", return_value=placed):
            for slip in body:
                assert client.post(f"/api/confirm/{slip['slip_id']}").status_code == 200

    def test_unresolvable_candidate_is_skipped_not_fatal(self, client):
        def resolve(event_id, *a, **kw):
            if event_id == "E2":
                raise ValueError("no market")
            return _market_ids(event_id)

        body = _prepare(client, resolve).json()
        assert [s["event_id"] for s in body] == ["E1", "E3"]

    def test_all_candidates_failing_still_returns_404(self, client):
        def resolve(event_id, *a, **kw):
            raise ValueError("no market")

        assert _prepare(client, resolve).status_code == 404

    def test_expired_session_still_surfaces_as_401(self, client):
        # SessionExpiredError must not be swallowed as "skip this candidate" —
        # the frontend depends on the 401 to re-show the login screen.
        app.dependency_overrides.clear()

        def resolve(event_id, *a, **kw):
            raise SessionExpiredError("expired")

        c = TestClient(app)
        with patch("backend.api.routes.get_token", return_value="tok"), \
             patch("backend.api.routes.AIInterpreter.interpret", return_value=INTERPRETED), \
             patch("backend.api.routes.find_event_candidates", return_value=CANDIDATES), \
             patch("backend.api.routes.get_market_types", return_value=["MATCH_ODDS"]), \
             patch("backend.api.routes.AIInterpreter.select_top_events", return_value=SELECTIONS), \
             patch("backend.api.routes.resolve_market", side_effect=resolve):
            assert c.post("/api/prepare", json={"user_input": "Back Arsenal 10"}).status_code == 401


# ── racing book prefetch ──────────────────────────────────────────────────────

RACING_BET = ParsedBet(
    selection_name="Constitution Hill", sport="Horse Racing", side="BACK",
    stake=20.0, market_type="WIN",
)
RACING_INTERPRETED = ClarificationResponse(status="ok", parsed_bet=RACING_BET)

RACING_MATCHES = [
    {
        "eventId": "E1", "marketId": "1.111", "selectionId": 101,
        "runnerName": "Constitution Hill", "competition": "Ascot 12th Jun",
        "eventName": "Ascot 12th Jun — 14:30", "marketStartTime": "2026-06-12T14:30:00Z",
        "marketType": "WIN", "places": None,
    },
    {
        "eventId": "E2", "marketId": "1.222", "selectionId": 201,
        "runnerName": "Constitution Hill", "competition": "Kempton 12th Jun",
        "eventName": "Kempton 12th Jun — 19:00", "marketStartTime": "2026-06-12T19:00:00Z",
        "marketType": "WIN", "places": None,
    },
]


class TestRacingBookPrefetch:
    def test_prefetched_book_is_handed_to_pricing(self, client):
        # The point of the prefetch: get_best_price must receive the already
        # fetched book, so it does not make a second listMarketBook call.
        books = {"1.111": {"status": "OPEN", "id": 1}, "1.222": {"status": "OPEN", "id": 2}}
        seen = []

        def price(market_id, selection_id, side, stake, session, line=None, book=None):
            seen.append((market_id, book))
            return 3.0

        with patch("backend.api.routes.AIInterpreter.interpret", return_value=RACING_INTERPRETED), \
             patch("backend.api.routes.resolve_racing_markets", return_value=deepcopy(RACING_MATCHES)), \
             patch("backend.api.routes.get_market_book", side_effect=lambda mid, s: books[mid]), \
             patch("backend.api.routes.get_best_price", side_effect=price):
            body = client.post("/api/prepare", json={"user_input": "back Constitution Hill 20"}).json()

        assert [s["market_id"] for s in body] == ["1.111", "1.222"]
        assert seen == [("1.111", books["1.111"]), ("1.222", books["1.222"])]

    def test_failed_prefetch_falls_back_to_fetching_inline(self, client):
        # A prefetch error must not lose the slip — book stays None and
        # get_best_price fetches its own, exactly as before.
        seen = []

        def price(market_id, selection_id, side, stake, session, line=None, book=None):
            seen.append(book)
            return 3.0

        with patch("backend.api.routes.AIInterpreter.interpret", return_value=RACING_INTERPRETED), \
             patch("backend.api.routes.resolve_racing_markets", return_value=deepcopy(RACING_MATCHES)), \
             patch("backend.api.routes.get_market_book", side_effect=ValueError("no book")), \
             patch("backend.api.routes.get_best_price", side_effect=price):
            body = client.post("/api/prepare", json={"user_input": "back Constitution Hill 20"}).json()

        assert len(body) == 2
        assert seen == [None, None]


# ── concurrent place-market selection ─────────────────────────────────────────

def _place_market(market_id, event_id, name, runner_id):
    return {
        "marketId": market_id,
        "marketName": name,
        "marketStartTime": "2026-06-12T14:30:00Z",
        "description": {"marketType": "PLACE"},
        "event": {"id": event_id, "name": f"Meeting {event_id}"},
        "runners": [{"selectionId": runner_id, "runnerName": "Swift Dancer"}],
    }


class TestConcurrentPlaceSelection:
    def test_place_refinement_preserves_race_order(self):
        pools = {
            "E1": [_place_market("1.1", "E1", "To Be Placed", 11)],
            "E2": [_place_market("1.2", "E2", "To Be Placed", 22)],
            "E3": [_place_market("1.3", "E3", "To Be Placed", 33)],
        }
        # Slowest first, so a naive as-completed gather would reorder the races.
        delays = {"E1": 0.06, "E2": 0.03, "E3": 0.0}

        def fake_list(event_id, session):
            time.sleep(delays[event_id])
            return pools[event_id]

        bet = ParsedBet(selection_name="Swift Dancer", sport="Horse Racing",
                        side="BACK", stake=10.0, market_type="PLACE")
        scan = [_place_market(f"1.{i}", f"E{i}", "To Be Placed", i * 11)
                for i in (1, 2, 3)]

        with patch.object(racing_service, "list_racing_markets", return_value=scan), \
             patch.object(racing_service, "list_place_markets_for_event", side_effect=fake_list), \
             patch.object(racing_service, "get_market_winners",
                          side_effect=lambda ids, s: dict.fromkeys(ids, 3)):
            matches = racing_service.resolve_racing_markets(bet, "Swift Dancer to place 10", {})

        assert [m["eventId"] for m in matches] == ["E1", "E2", "E3"]
        assert all(m["places"] == 3 for m in matches)

# NB the SQLite connection-reuse tests live in test_logger_connection.py — the
# autouse mock_logger fixture in conftest stubs the logger out for every file
# whose name doesn't contain "test_logger", which would make them vacuous here.
