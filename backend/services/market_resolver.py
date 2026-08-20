"""Runner-name matching for a single market's runner list.

Three passes, strictest first. The order matters and is measured — see
`python -m backend.eval match`, which scores this against the previous
loose-matching implementation over a corpus seeded from real logged requests.

  1. exact      — the runner named exactly as asked wins, wherever it sits in the
                  list. Without this the answer depends on Betfair's catalogue
                  order: "Shallow" resolved to "Shallow Hal" purely because that
                  runner was listed first.
  2. forward    — the request appears inside a runner name ("Scottie" ->
                  "Scottie Scheffler"). Keeps partial names working.
  3. reverse    — a runner name appears inside a longer request ("Northampton
                  Saints" -> the runner "Northampton"), but only on whole-word
                  boundaries AND only when exactly one runner qualifies.

The guard on pass 3 is the important one. Unbounded, it let any short runner name
swallow a longer request, and it silently resolved requests naming two
participants: "Fury v Joshua to go the distance" (a real logged request) matched
the runner "Joshua" simply because Betfair listed that fighter first — a bet on
the opponent. Returning no match instead hands those to the AI runner fallback,
which sees the full user input and can tell the two apart.
"""
import re


def _word_bounded(needle: str, haystack: str) -> bool:
    """True if `needle` occurs in `haystack` on whole-word boundaries."""
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def resolve_selection(runners: list, selection_name: str) -> tuple[int, str] | tuple[None, None]:
    target = selection_name.lower().strip()

    for runner in runners:
        if (runner.get("runnerName") or "").lower().strip() == target:
            return runner["selectionId"], runner.get("runnerName", "")

    for runner in runners:
        name = (runner.get("runnerName") or "").lower()
        if name and target in name:
            return runner["selectionId"], runner.get("runnerName", "")

    reverse_hits = [
        runner for runner in runners
        if _word_bounded((runner.get("runnerName") or "").lower().strip(), target)
    ]
    if len(reverse_hits) == 1:
        return reverse_hits[0]["selectionId"], reverse_hits[0].get("runnerName", "")

    return None, None
