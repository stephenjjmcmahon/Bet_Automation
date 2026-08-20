"""Offline accuracy harness.

Two layers, both deterministic and free (no Betfair calls, no LLM calls):

  `match`  — runs candidate runner-matching strategies over a corpus of
             (runners, selection_name) cases and scores them head to head, so a
             proposed change to the matcher is a measured delta rather than an
             argument.
  `parses` — audits the real parse log (logs/interpreter.jsonl) against the
             app's own config, finding parses that the downstream pipeline
             cannot resolve regardless of how good the matcher is.

Run:  python -m backend.eval match
      python -m backend.eval parses
"""
