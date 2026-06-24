"""Deterministic, budget-bounded tools over the Betfair API for the search agent.

These wrap the betfair_client navigation/pricing calls and enforce the cost
controls from the design:
  - navigation (find_events / list_market_types / list_markets) is cheap and
    returns no prices;
  - pricing (price_markets) is the only expensive call, capped to the most-traded
    MAX_PRICED_MARKETS so a broad query can never blow Betfair's data limits.

A SearchTools instance is request-scoped. list_markets caches each market's
metadata (including totalMatched and runner names), so price_markets can both
rank by liquidity before truncating and join runner names onto the book — the
market book itself only carries selectionIds, not names.

The same instance backs the deterministic browse endpoints (event drill-through),
which call list_markets / price_markets directly without the agent.
"""
from backend.services.betfair_client import (
    list_events_filtered,
    list_market_books,
    list_market_types_for_events,
    list_markets_for_events,
)
from backend.config.sport_mapping import event_type_id_for, UnsupportedSportError

# Most events handed back to the agent — enough to cover a day's fixtures for a
# competition without flooding the model's context.
MAX_EVENTS = 50

# Liquidity-ranked pricing budget: the most-traded N markets are priced, the rest
# are reported as truncated. 80 = two listMarketBook calls (40 each, the
# EX_BEST_OFFERS weight cap).
MAX_PRICED_MARKETS = 80

# Betfair's price floor. A back offer at or below this is not a real market price
# (mirrors odds_service.get_best_price), so we surface no back price for it.
PRICE_FLOOR = 1.01


def _best(offers: list) -> tuple:
    """Top-of-book (price, size) from an availableToBack/Lay list, or (None, None)."""
    if offers:
        return offers[0].get("price"), offers[0].get("size")
    return None, None


class SearchTools:
    def __init__(self, session: dict):
        self.session = session
        # market_id -> catalogue metadata (incl. total_matched + runner names),
        # populated by list_markets and consumed by price_markets.
        self._markets: dict[str, dict] = {}
        # event_id -> compact event dict, populated by find_events so the agent can
        # present events as a navigable list (by id) without re-fetching.
        self._events: dict[str, dict] = {}

    # ── navigation (cheap, no prices) ──────────────────────────────────────────

    def find_events(self, sport: str, text: str = None,
                    time_from: str = None, time_to: str = None,
                    country: str = None) -> list:
        """Events for a sport, optionally narrowed by text, a start-time window,
        and/or a country (ISO 3166-1 alpha-2 code, e.g. "AU"). Returns up to
        MAX_EVENTS compact event dicts."""
        try:
            event_type_id = event_type_id_for(sport)
        except UnsupportedSportError:
            return []
        events = list_events_filtered(
            event_type_id, self.session,
            text_query=text, time_from=time_from, time_to=time_to,
            countries=[country] if country else None,
        )
        out = []
        for ev in events[:MAX_EVENTS]:
            e = ev.get("event", {})
            row = {
                "event_id": e.get("id"),
                "name": e.get("name"),
                "open_date": e.get("openDate"),
                "competition": (ev.get("competition") or {}).get("name"),
            }
            if row["event_id"]:
                self._events[row["event_id"]] = row
            out.append(row)
        return out

    def list_market_types(self, event_ids: list) -> list:
        """Distinct market type codes available across the given events."""
        if not event_ids:
            return []
        seen = []
        for t in list_market_types_for_events(event_ids, self.session):
            if t not in seen:
                seen.append(t)
        return seen

    def list_markets(self, event_ids: list, market_types: list = None) -> list:
        """Markets across the events, liquidity-ranked, no prices. Caches each
        market's metadata for price_markets to rank and join against."""
        if not event_ids:
            return []
        markets = list_markets_for_events(
            event_ids, self.session, market_type_codes=market_types,
        )
        out = []
        for m in markets:
            meta = self._market_meta(m)
            if meta["market_id"]:
                self._markets[meta["market_id"]] = meta
            out.append(meta)
        return out

    # ── pricing (expensive, budget-capped) ─────────────────────────────────────

    def price_markets(self, market_ids: list) -> dict:
        """Live prices for the requested markets, capped to the MAX_PRICED_MARKETS
        most-traded (by cached totalMatched) so the data budget is never exceeded.

        Returns {markets, total, shown, truncated}. Each market carries its ACTIVE
        runners with best back/lay price + size, joined to catalogue runner names.
        SUSPENDED markets keep their status so the caller can flag them.
        """
        if not market_ids:
            return {"markets": [], "total": 0, "shown": 0, "truncated": False}

        ranked = sorted(
            market_ids,
            key=lambda mid: self._markets.get(mid, {}).get("total_matched", 0),
            reverse=True,
        )
        shown_ids = ranked[:MAX_PRICED_MARKETS]

        books = list_market_books(shown_ids, self.session)
        books_by_id = {b["marketId"]: b for b in books}

        priced = []
        for mid in shown_ids:
            meta = self._markets.get(mid)
            book = books_by_id.get(mid)
            if meta is None or book is None:
                # Uncached (not seen via list_markets) or no book returned — skip
                # rather than emit a nameless/typeless row.
                continue
            priced.append(self._priced_market(meta, book))

        return {
            "markets": priced,
            "total": len(market_ids),
            "shown": len(priced),
            "truncated": len(market_ids) > len(shown_ids),
        }

    # ── helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _market_meta(m: dict) -> dict:
        return {
            "market_id": m.get("marketId"),
            "market_name": m.get("marketName"),
            "market_type": (m.get("description") or {}).get("marketType"),
            "event_id": (m.get("event") or {}).get("id"),
            "event_name": (m.get("event") or {}).get("name"),
            "competition": (m.get("competition") or {}).get("name"),
            "market_start_time": m.get("marketStartTime"),
            "total_matched": m.get("totalMatched") or 0,
            "runners": [
                {
                    "selection_id": r["selectionId"],
                    "runner_name": r.get("runnerName"),
                    "handicap": r.get("handicap"),
                }
                for r in m.get("runners", [])
            ],
        }

    @staticmethod
    def _priced_market(meta: dict, book: dict) -> dict:
        names = {r["selection_id"]: r for r in meta["runners"]}
        runners = []
        for br in book.get("runners", []):
            if br.get("status") != "ACTIVE":
                continue
            sid = br["selectionId"]
            ex = br.get("ex", {})
            back_price, back_size = _best(ex.get("availableToBack", []))
            lay_price, lay_size = _best(ex.get("availableToLay", []))
            if back_price is not None and back_price <= PRICE_FLOOR:
                back_price, back_size = None, None
            cat = names.get(sid, {})
            runners.append({
                "selection_id": sid,
                "runner_name": cat.get("runner_name"),
                "handicap": br.get("handicap") or cat.get("handicap"),
                "back_price": back_price,
                "back_size": back_size,
                "lay_price": lay_price,
                "lay_size": lay_size,
            })
        return {
            "market_id": meta["market_id"],
            "market_name": meta["market_name"],
            "market_type": meta["market_type"],
            "event_id": meta["event_id"],
            "event_name": meta["event_name"],
            "competition": meta["competition"],
            "market_start_time": meta["market_start_time"],
            "total_matched": meta["total_matched"],
            "status": book.get("status"),
            "runners": runners,
        }
