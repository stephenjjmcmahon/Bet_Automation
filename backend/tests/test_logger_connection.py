"""Tests for the reused SQLite connection in backend.services.logger.

The connection used to be opened (with schema setup) and closed on every single
write, including inside the request path. It is now opened once per thread per
DB_PATH and kept. These tests pin the behaviour that has to survive that:
commits are still visible to other connections, a monkeypatched DB_PATH still
redirects writes, and threads never share a handle.

The filename must contain "test_logger" — conftest's autouse mock_logger fixture
stubs the logger out for every other file, which would make all of this vacuous.
"""
import sqlite3

from backend.services import logger
from backend.services.concurrency import parallel_map


def test_same_connection_is_reused_across_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(logger, "DB_PATH", tmp_path / "a.db")
    logger.log_failure(reason="no_matching_event")
    first = logger._get_conn()
    logger.log_failure(reason="market_suspended")
    assert logger._get_conn() is first


def test_switching_db_path_reopens(tmp_path, monkeypatch):
    monkeypatch.setattr(logger, "DB_PATH", tmp_path / "a.db")
    logger.log_failure(reason="no_matching_event")
    logger.log_failure(reason="market_suspended")
    first = logger._get_conn()

    monkeypatch.setattr(logger, "DB_PATH", tmp_path / "b.db")
    logger.log_failure(reason="no_matching_event")
    second = logger._get_conn()

    assert second is not first
    # Each database got only its own rows — the cached handle didn't leak writes.
    for path, expected in ((tmp_path / "a.db", 2), (tmp_path / "b.db", 1)):
        conn = sqlite3.connect(str(path))
        assert conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == expected
        conn.close()


def test_writes_are_committed_and_visible_to_other_connections(tmp_path, monkeypatch):
    # The connection is long-lived now, so an uncommitted write would stay
    # invisible to readers indefinitely rather than only until close().
    monkeypatch.setattr(logger, "DB_PATH", tmp_path / "c.db")
    logger.log_slip_prepared(
        slip_id="s1", time_to_slip_ms=10, selection_name="Arsenal",
        side="BACK", stake=10.0, price=2.0, market_id="1.1", event_id="E1",
    )
    logger.log_bet_confirmed(slip_id="s1", time_to_confirm_ms=5)

    conn = sqlite3.connect(str(tmp_path / "c.db"))
    row = conn.execute("SELECT status FROM bets WHERE slip_id='s1'").fetchone()
    conn.close()
    assert row[0] == "confirmed"


def test_log_search_still_returns_the_new_row_id(tmp_path, monkeypatch):
    # lastrowid is read before the commit-and-return refactor's early return —
    # the frontend attaches thumbs up/down to this id.
    monkeypatch.setattr(logger, "DB_PATH", tmp_path / "e.db")
    kwargs = {
        "rounds": 2, "hit_round_cap": False, "price_calls": 1,
        "total_latency_ms": 100, "llm_latency_ms": 50, "cards": 3,
        "events": 0, "salvaged": False,
    }
    first = logger.log_search(query="tonight's football", **kwargs)
    second = logger.log_search(query="tomorrow's racing", **kwargs)
    assert (first, second) == (1, 2)

    logger.log_search_feedback(second, correct=True)
    conn = sqlite3.connect(str(tmp_path / "e.db"))
    assert conn.execute("SELECT feedback FROM searches WHERE id=?", (second,)).fetchone()[0] == 1
    conn.close()


def test_a_failed_write_does_not_poison_later_writes(tmp_path, monkeypatch):
    # With a per-write connection a failure was thrown away with the connection.
    # The handle is reused now, so the failed statement must be rolled back or
    # every subsequent write on this thread would be stuck behind it.
    monkeypatch.setattr(logger, "DB_PATH", tmp_path / "f.db")
    logger.log_failure(reason="first")

    # reason is NOT NULL — this insert fails inside the _write block.
    try:
        logger.log_failure(reason=None)
    except sqlite3.IntegrityError:
        pass

    logger.log_failure(reason="after")

    conn = sqlite3.connect(str(tmp_path / "f.db"))
    reasons = [r[0] for r in conn.execute("SELECT reason FROM failures ORDER BY id").fetchall()]
    conn.close()
    assert reasons == ["first", "after"]


def test_each_thread_gets_its_own_connection(tmp_path, monkeypatch):
    # sqlite3 connections may not be shared across threads by default, and
    # FastAPI runs these sync endpoints in a worker threadpool.
    monkeypatch.setattr(logger, "DB_PATH", tmp_path / "d.db")
    logger.log_failure(reason="no_matching_event")
    main_conn = logger._get_conn()

    results = parallel_map(
        lambda _: (logger.log_failure(reason="threaded"), logger._get_conn())[1],
        [1, 2, 3],
    )
    assert all(exc is None for _, exc in results)
    conns = [c for c, _ in results]
    assert all(c is not main_conn for c in conns)

    conn = sqlite3.connect(str(tmp_path / "d.db"))
    assert conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 4
    conn.close()
