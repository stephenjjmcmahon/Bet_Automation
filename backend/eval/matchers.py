"""Runner-matching strategies, scored head to head by the harness.

`current` is the SHIPPED market_resolver.resolve_selection, imported rather than
copied so it can never drift from the real code.

`legacy_loose` is a frozen copy of the algorithm that shipped before the matcher
was reworked. It is the baseline on purpose: it keeps the corpus explaining what
the change bought, and it fails the build if anyone reverts to that behaviour.
Do not "fix" it — it is a historical record, not live code.

To evaluate a new idea, add a function here and register it in MATCHERS; the
harness will score it and, more importantly, report any case it regresses.
"""
from backend.services.market_resolver import resolve_selection

NO_MATCH = (None, None)


def legacy_loose(runners: list, selection_name: str) -> tuple:
    """FROZEN pre-rework behaviour: the first runner whose name contains, or is
    contained by, the selection. No exact preference, no word boundaries, so the
    result depends on Betfair's catalogue order."""
    name_lower = selection_name.lower()
    for runner in runners:
        runner_name = runner.get("runnerName", "")
        if name_lower in runner_name.lower() or runner_name.lower() in name_lower:
            return runner["selectionId"], runner_name
    return NO_MATCH


def exact_first(runners: list, selection_name: str) -> tuple:
    """Intermediate step, kept for the record: exact match preferred, then the
    old loose fallback. Shows how much the exact pass alone is worth versus the
    word-boundary guard that `current` adds on top."""
    target = selection_name.lower().strip()
    for runner in runners:
        if (runner.get("runnerName") or "").lower().strip() == target:
            return runner["selectionId"], runner.get("runnerName", "")
    return legacy_loose(runners, selection_name)


def current(runners: list, selection_name: str) -> tuple:
    """Shipped behaviour — exact, then forward containment, then a unique
    whole-word reverse match. See market_resolver for the reasoning."""
    return resolve_selection(runners, selection_name)


MATCHERS = {
    "legacy_loose": legacy_loose,
    "exact_first": exact_first,
    "current": current,
}

# The historical algorithm, so "fixes" and "regressions" are measured against
# what the app used to do rather than against whatever it does today.
BASELINE = "legacy_loose"
