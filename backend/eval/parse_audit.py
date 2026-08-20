"""Audits logs/interpreter.jsonl against the app's own configuration.

Every check here is a parse that the pipeline downstream CANNOT resolve however
good the runner matcher is — an unmapped sport, a racing market type that
silently degrades to WIN, a line market with no line. These are grounded in real
traffic (the log is what the app was actually asked), so counts here are field
frequencies, not estimates.

No network, no LLM: it reads the log and the config modules only.
"""
import json
from collections import Counter
from pathlib import Path

from backend.config.sport_mapping import RACING_SPORTS, SPORT_EVENT_TYPE_MAP
from backend.services.racing_service import (
    RACING_UNSUPPORTED_MESSAGES,
    UnsupportedRacingMarketError,
    racing_market_for,
)

LOG_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "interpreter.jsonl"

# Market types whose resolution keys off ParsedBet.line. Without a line the
# COMBINED_TOTAL row lookup in get_best_price cannot select a row.
LINE_DEPENDENT = {"COMBINED_TOTAL", "TOTAL_MATCH_POINTS"}


def load(path: Path = LOG_PATH) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def audit(rows: list) -> dict:
    parsed = [r for r in rows if (r.get("output") or {}).get("status") == "ok"]

    findings = {
        "unmapped_sport": [],
        "racing_market_unresolvable": [],
        "racing_market_declined": [],
        "racing_places_recovered": [],
        "line_market_without_line": [],
        "empty_selection_name": [],
        "selection_contains_opponent_marker": [],
    }

    for r in parsed:
        out = r["output"]
        user_input = r.get("input", "")
        sport = (out.get("sport") or "").strip()
        selection = (out.get("selection_name") or "").strip()
        market_type = (out.get("market_type") or "").strip().upper()

        if sport and sport.lower() not in SPORT_EVENT_TYPE_MAP:
            findings["unmapped_sport"].append((user_input, sport))

        if sport.lower() in RACING_SPORTS:
            if market_type in RACING_UNSUPPORTED_MESSAGES:
                findings["racing_market_declined"].append((user_input, market_type))
            else:
                # Ask the real mapping, so this audit can never disagree with what
                # the app actually does.
                try:
                    resolved, resolved_places = racing_market_for(
                        market_type, out.get("places")
                    )
                except UnsupportedRacingMarketError:
                    findings["racing_market_unresolvable"].append((user_input, market_type))
                else:
                    if out.get("places") is None and resolved_places is not None:
                        findings["racing_places_recovered"].append(
                            (user_input, f"{market_type} -> {resolved} x{resolved_places}")
                        )

        if market_type in LINE_DEPENDENT and out.get("line") is None:
            findings["line_market_without_line"].append((user_input, market_type))

        if not selection:
            findings["empty_selection_name"].append((user_input, market_type))

        # A selection naming two participants can't be matched to one runner —
        # the shipped matcher picks whichever is listed first.
        low = f" {selection.lower()} "
        if any(tok in low for tok in (" v ", " vs ", " versus ")):
            findings["selection_contains_opponent_marker"].append((user_input, selection))

    return {"parsed": len(parsed), "total": len(rows), "findings": findings}


DESCRIPTIONS = {
    "unmapped_sport":
        "sport has no Betfair event-type id -> UnsupportedSportError, request fails",
    "racing_market_unresolvable":
        "racing market type the mapping refuses -> declined cleanly (was: silent WIN bet)",
    "racing_market_declined":
        "racing market type deliberately declined (expected, not a defect)",
    "racing_places_recovered":
        "places count recovered from the market type code (expected, not a defect)",
    "line_market_without_line":
        "line-dependent market parsed with line=null -> cannot select a row",
    "empty_selection_name":
        "no selection name to match against",
    "selection_contains_opponent_marker":
        "selection names two participants -> no confident match, AI fallback decides",
}


def report() -> int:
    rows = load()
    if not rows:
        print(f"No parse log found at {LOG_PATH} — nothing to audit.")
        return 0

    result = audit(rows)
    print(f"Parse log: {LOG_PATH}")
    print(f"  {result['total']} entries, {result['parsed']} successful parses")
    print()

    findings = result["findings"]
    width = max(len(k) for k in findings)
    print(f"{'check':<{width}}  count  effect")
    print("-" * (width + 60))
    for key, hits in findings.items():
        print(f"{key:<{width}}  {len(hits):>5}  {DESCRIPTIONS[key]}")
    print()

    for key, hits in findings.items():
        if not hits:
            continue
        print(f"{key} ({len(hits)}):")
        for user_input, detail in list(dict.fromkeys(hits))[:10]:
            print(f"    {detail!r:<28} <- {user_input!r}")
        if len(set(hits)) > 10:
            print(f"    ... {len(set(hits)) - 10} more distinct")
        print()

    expected = {"racing_market_declined", "racing_places_recovered"}
    counts = Counter({k: len(v) for k, v in findings.items()})
    actionable = sum(n for k, n in counts.items() if k not in expected)
    print(f"{actionable} parse(s) out of {result['parsed']} hit a pipeline gap "
          f"that the runner matcher cannot fix.")
    return 0
