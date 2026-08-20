"""Scores every matcher in matchers.MATCHERS over cases.CASES.

The headline number is not the pass rate — it is the WIN/REGRESSION split
against the baseline. A candidate that fixes five cases and breaks one is not
obviously better, and the report says so explicitly instead of hiding it in an
aggregate.
"""
from backend.eval import cases as cases_mod
from backend.eval.matchers import BASELINE, MATCHERS


def run_matcher(fn, case) -> tuple:
    """(selection_id, passed) for one matcher on one case."""
    try:
        sid, _name = fn(list(case.runners), case.selection_name)
    except Exception as e:   # a matcher must never explode on real input
        return f"ERROR: {type(e).__name__}: {e}", False
    return sid, sid == case.expected


def evaluate() -> dict:
    """{matcher_name: {case_id: (result, passed)}}"""
    return {
        name: {c.id: run_matcher(fn, c) for c in cases_mod.CASES}
        for name, fn in MATCHERS.items()
    }


def _fmt(value) -> str:
    return "no match" if value is None else str(value)


def report() -> int:
    results = evaluate()
    total = len(cases_mod.CASES)
    baseline = results[BASELINE]

    print(f"Runner-matching corpus: {total} cases")
    for source, n in sorted(cases_mod.by_source().items(), key=lambda kv: -kv[1]):
        print(f"  {n:>3}  {source}")
    print()

    print(f"{'matcher':<22}{'passed':>10}{'fixes':>8}{'breaks':>9}")
    print("-" * 49)
    summary = {}
    for name, res in results.items():
        passed = sum(1 for _, ok in res.values() if ok)
        fixes = sum(
            1 for cid, (_, ok) in res.items() if ok and not baseline[cid][1]
        )
        breaks = sum(
            1 for cid, (_, ok) in res.items() if not ok and baseline[cid][1]
        )
        summary[name] = (passed, fixes, breaks)
        marker = "  (baseline)" if name == BASELINE else ""
        print(f"{name:<22}{passed:>4}/{total:<5}{fixes:>8}{breaks:>9}{marker}")
    print()

    # Per-case detail wherever the matchers disagree, or everyone fails.
    interesting = [
        c for c in cases_mod.CASES
        if len({results[m][c.id][1] for m in results}) > 1
        or not any(results[m][c.id][1] for m in results)
    ]
    if interesting:
        print("Cases where matchers disagree (or all fail):")
        print()
        for c in interesting:
            flag = " [ambiguous]" if c.ambiguous else ""
            print(f"  {c.id}  ({c.source}){flag}")
            print(f"    asked for : {c.selection_name!r}")
            print(f"    runners   : {[r['runnerName'] for r in c.runners]}")
            print(f"    expected  : {_fmt(c.expected)}")
            for name in results:
                got, ok = results[name][c.id]
                print(f"      {'PASS' if ok else 'FAIL'}  {name:<20} -> {_fmt(got)}")
            if c.note:
                print(f"    note: {c.note}")
            print()

    best = max(summary.items(), key=lambda kv: (kv[1][0], -kv[1][2]))
    print(f"Best on this corpus: {best[0]} "
          f"({best[1][0]}/{total} passed, {best[1][1]} fixes, {best[1][2]} regressions)")
    return 0
