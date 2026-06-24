"""Tests for the search tool surface (search_tools.SearchTools) and the batched
market-book client helper.

Focus on the cost guardrails that the agent and the browse endpoints both rely
on: the pricing budget (cap + liquidity-ranked truncation), the join of book
prices onto catalogue runner names, dropping inactive runners, the 1.01 floor,
and that listMarketBook is batched within Betfair's 40-market weight cap.
"""
from unittest.mock import patch

from backend.services import betfair_client, search_tools
from backend.services.search_tools import SearchTools, MAX_PRICED_MARKETS


def _cat_market(mid, total, runners=None, mtype="MATCH_ODDS"):
    return {
        "marketId": mid,
        "marketName": mtype,
        "totalMatched": total,
        "description": {"marketType": mtype},
        "event": {"id": "e1", "name": "A v B"},
        "competition": {"name": "Comp"},
        "marketStartTime": "2026-06-17T19:00:00Z",
        "runners": runners or [{"selectionId": 1, "runnerName": "A", "handicap": 0.0}],
    }


def _book(mid, runners):
    return {"marketId": mid, "status": "OPEN", "runners": runners}


def _brunner(sid, status="ACTIVE", back=2.0, size=100.0, lay=None, handicap=0.0):
    ex = {
        "availableToBack": [{"price": back, "size": size}] if back is not None else [],
        "availableToLay": [{"price": lay, "size": size}] if lay is not None else [],
    }
    return {"selectionId": sid, "status": status, "handicap": handicap, "ex": ex}


def test_price_markets_caps_and_ranks_by_liquidity():
    # total_matched increases with index, so the lowest-index markets are the
    # least liquid and must be the ones dropped when the budget truncates.
    cats = [_cat_market(f"m{i}", total=i) for i in range(MAX_PRICED_MARKETS + 5)]
    tools = SearchTools({})
    with patch.object(search_tools, "list_markets_for_events", return_value=cats):
        tools.list_markets(["e1"])

    captured = {}

    def fake_books(ids, session):
        captured["ids"] = list(ids)
        return [_book(mid, [_brunner(1)]) for mid in ids]

    ids = [f"m{i}" for i in range(MAX_PRICED_MARKETS + 5)]
    with patch.object(search_tools, "list_market_books", side_effect=fake_books):
        res = tools.price_markets(ids)

    assert res["total"] == MAX_PRICED_MARKETS + 5
    assert res["shown"] == MAX_PRICED_MARKETS
    assert res["truncated"] is True
    # least-liquid dropped, most-liquid kept
    assert "m0" not in captured["ids"]
    assert f"m{MAX_PRICED_MARKETS + 4}" in captured["ids"]


def test_price_markets_joins_names_drops_inactive_and_applies_floor():
    cat = _cat_market("m1", total=100, runners=[
        {"selectionId": 1, "runnerName": "Over 2.5", "handicap": 0.0},
        {"selectionId": 2, "runnerName": "Under 2.5", "handicap": 0.0},
        {"selectionId": 3, "runnerName": "Withdrawn", "handicap": 0.0},
    ])
    tools = SearchTools({})
    with patch.object(search_tools, "list_markets_for_events", return_value=[cat]):
        tools.list_markets(["e1"])

    book = _book("m1", [
        _brunner(1, back=1.95, size=420.0, lay=1.98),
        _brunner(2, back=1.01, size=5.0),     # at the floor -> no real back price
        _brunner(3, status="REMOVED"),         # inactive -> dropped
    ])
    with patch.object(search_tools, "list_market_books", return_value=[book]):
        res = tools.price_markets(["m1"])

    runners = res["markets"][0]["runners"]
    assert len(runners) == 2  # the REMOVED runner is gone
    r1 = next(r for r in runners if r["selection_id"] == 1)
    assert r1["runner_name"] == "Over 2.5"
    assert r1["back_price"] == 1.95
    assert r1["back_size"] == 420.0
    assert r1["lay_price"] == 1.98
    r2 = next(r for r in runners if r["selection_id"] == 2)
    assert r2["back_price"] is None  # floored out


def test_price_markets_skips_uncached_markets():
    # An id never seen via list_markets has no name/type metadata, so it is
    # skipped rather than emitted as a nameless row.
    tools = SearchTools({})
    with patch.object(search_tools, "list_market_books",
                      return_value=[_book("ghost", [_brunner(1)])]):
        res = tools.price_markets(["ghost"])
    assert res["markets"] == []
    assert res["shown"] == 0


def test_list_market_books_batches_by_40():
    ids = [f"m{i}" for i in range(95)]
    calls = []

    def fake_post(path, payload, session):
        calls.append(payload["marketIds"])
        return [{"marketId": mid} for mid in payload["marketIds"]]

    with patch.object(betfair_client, "betfair_post", side_effect=fake_post):
        books = betfair_client.list_market_books(ids, {})

    assert [len(c) for c in calls] == [40, 40, 15]
    assert len(books) == 95
