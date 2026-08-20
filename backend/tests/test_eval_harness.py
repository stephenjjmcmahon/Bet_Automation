"""Guards for the accuracy harness itself.

The harness is only useful if it keeps telling the truth about the shipped code,
so these pin the properties its conclusions rest on rather than any headline
number: the baseline really is the shipped matcher, no candidate regresses a
case the baseline passes, and the log audit reads real config.
"""
import pytest

from backend.eval import cases as cases_mod
from backend.eval import parse_audit
from backend.eval.harness import evaluate
from backend.eval.matchers import BASELINE, MATCHERS
from backend.services.market_resolver import resolve_selection


def test_baseline_matcher_is_the_shipped_resolver():
    # If someone rewrites resolve_selection, the baseline must move with it —
    # otherwise the harness scores a candidate against a stale copy.
    runners = [{"selectionId": 7, "runnerName": "Leinster"}]
    assert MATCHERS[BASELINE](runners, "Leinster") == resolve_selection(runners, "Leinster")


def test_every_case_has_a_recognised_source():
    allowed = {"logged", "documented", "synthetic", "repo-fixture"}
    assert {c.source for c in cases_mod.CASES} <= allowed


def test_case_ids_are_unique():
    ids = [c.id for c in cases_mod.CASES]
    assert len(ids) == len(set(ids))


def test_expected_selection_id_exists_in_the_case_runners():
    # An expectation pointing at an id that isn't in the market would be
    # unreachable by any matcher, making the case meaningless.
    for c in cases_mod.CASES:
        if c.expected is not None:
            assert c.expected in {r["selectionId"] for r in c.runners}, c.id


@pytest.mark.parametrize("name", [n for n in MATCHERS if n != BASELINE])
def test_candidate_matchers_never_regress_a_baseline_pass(name):
    results = evaluate()
    baseline = results[BASELINE]
    regressions = [
        cid for cid, (_, ok) in results[name].items() if not ok and baseline[cid][1]
    ]
    assert regressions == [], f"{name} regresses: {regressions}"


def test_no_matcher_raises_on_any_case():
    for name, res in evaluate().items():
        for cid, (result, _) in res.items():
            assert not str(result).startswith("ERROR:"), f"{name} raised on {cid}: {result}"


def test_reverse_containment_still_reaches_a_tersely_named_runner():
    # The single most important non-regression: "Northampton Saints" must still
    # resolve to a runner named just "Northampton", or fixing the reverse arm
    # would break ordinary team bets.
    runners = [{"selectionId": 1, "runnerName": "Northampton"},
               {"selectionId": 2, "runnerName": "Exeter"}]
    for name, fn in MATCHERS.items():
        assert fn(runners, "Northampton Saints")[0] == 1, name


def test_parse_audit_runs_on_a_synthetic_log():
    rows = [
        {"input": "back Dobbin top 3 at York 5",
         "output": {"status": "ok", "selection_name": "Dobbin", "sport": "Horse Racing",
                    "market_type": "TOP_3_FINISH", "stake": 5.0}},
        {"input": "forecast Dobbin then Trigger 5",
         "output": {"status": "ok", "selection_name": "Dobbin", "sport": "Horse Racing",
                    "market_type": "FORECAST", "stake": 5.0}},
        {"input": "back Dobbin in the 3.30 novelty market 5",
         "output": {"status": "ok", "selection_name": "Dobbin", "sport": "Horse Racing",
                    "market_type": "SOME_FUTURE_MARKET", "stake": 5.0}},
        {"input": "back Leinster 10",
         "output": {"status": "ok", "selection_name": "Leinster", "sport": "Rugby Union",
                    "market_type": "MATCH_ODDS", "stake": 10.0}},
        {"input": "back nothing",
         "output": {"status": "clarification_needed"}},
    ]
    result = parse_audit.audit(rows)
    findings = result["findings"]

    assert result["parsed"] == 4
    # TOP_3_FINISH now resolves to a 3-place PLACE bet rather than a silent WIN.
    assert len(findings["racing_places_recovered"]) == 1
    assert len(findings["racing_market_declined"]) == 1      # FORECAST
    assert len(findings["racing_market_unresolvable"]) == 1  # the unknown label
    assert findings["unmapped_sport"] == []


def test_parse_audit_tracks_the_real_mapping():
    # The audit must report what racing_market_for actually does. If someone adds
    # a mapping, the audit has to stop flagging it without being edited.
    rows = [{"input": "back Dobbin each way 5",
             "output": {"status": "ok", "selection_name": "Dobbin",
                        "sport": "Horse Racing", "market_type": "EACH_WAY", "stake": 5.0}}]
    findings = parse_audit.audit(rows)["findings"]
    assert findings["racing_market_unresolvable"] == []


def test_parse_audit_handles_a_missing_log(tmp_path):
    assert parse_audit.load(tmp_path / "nope.jsonl") == []
