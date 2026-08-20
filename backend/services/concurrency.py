"""Bounded parallel map for the I/O-bound Betfair fan-outs.

The bet pipeline resolves up to MAX_RACING_MATCHES / top-3 candidates, and each
one is an independent chain of Betfair calls (catalogue + market book, sometimes
an LLM runner match). Run serially that is 3x the round trips of the slowest one;
they share nothing, so they can overlap.

Deliberately *not* a general-purpose utility:

  - Results come back in input order, so callers keep their existing ranking
    (best candidate first) without re-sorting.
  - Exceptions are captured and returned alongside results rather than raised, so
    each caller keeps its own per-item error handling exactly as it was when the
    loop was serial (skip this candidate vs. let it propagate to a 401).
  - Only pure I/O fan-out belongs here. Anything that mutates the user's session
    dict must stay on the calling thread — concurrent read-modify-write of the
    session would drop pending slips.
"""
from concurrent.futures import ThreadPoolExecutor

# Matches the widest fan-out the callers produce (3 racing matches / 3 ranked
# event candidates) — enough to overlap them, small enough to stay polite to
# Betfair and to fit the client's connection pool.
MAX_WORKERS = 4


def parallel_map(fn, items: list) -> list:
    """Apply `fn` to every item concurrently. Returns [(result, exception), ...]
    in input order; exactly one of the pair is None for each item.

    Falls back to a plain in-line call for 0 or 1 items so the common
    single-candidate case pays no thread overhead at all.
    """
    if not items:
        return []

    if len(items) == 1:
        try:
            return [(fn(items[0]), None)]
        except Exception as e:   # noqa: BLE001 — handed back to the caller verbatim
            return [(None, e)]

    def _guarded(item):
        try:
            return fn(item), None
        except Exception as e:   # noqa: BLE001 — handed back to the caller verbatim
            return None, e

    with ThreadPoolExecutor(max_workers=min(len(items), MAX_WORKERS)) as pool:
        return list(pool.map(_guarded, items))
