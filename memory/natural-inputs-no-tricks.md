---
name: natural-inputs-no-tricks
description: When testing slip creation, use natural human inputs — never engineer the input to force a market
metadata:
  type: feedback
---

When verifying that the betting app can create a slip for a given market type, the test
input must be **natural, human-like phrasing**. Do NOT craft inputs that explicitly name
the Betfair market ("in the half time result market", "the COMBINED_TOTAL market") to
guarantee the right market is chosen — that hides real parser bugs.

**Why:** The goal is that a normal person's plain-English bet resolves to the correct real
market. Tricking the input proves nothing about the product.

**How to apply:** Phrase bets the way a punter would ("Fortaleza to win the first half").
If that resolves to the wrong market type, fix the parser prompt in
[[ai-interpreter-market-prompt]] / resolver — never reword the input to force it.
