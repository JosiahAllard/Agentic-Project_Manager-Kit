---
name: doc-claims-discipline
description: Use when you record a finding or conclusion, withdraw one, trim or delete lines from a doc, or add a capability that earlier docs may say does not exist. Covers evidence-labelling, reversals living in the file, what a deletion silently removes, and stale absence claims.
---

# Findings and doc edits

1. Write the evidence next to the claim and let observed / measured / inferred / untested read off it; do not pick the label first. A claim that names its evidence ("one timestamp, nothing to compare against") cannot be upgraded in a retelling without deleting the evidence that contradicts it.
2. A finding that holds has a control, one changed variable, and a prediction written down before the run. A number carries its comparison, sample size and conditions.
3. A finding that falls is withdrawn in the file that claims it, in the same change: a claim left filed gets repeated as fact, even by whoever withdrew it.
4. Before deleting or trimming a bullet, list every rule in it; say in the change what the deletion removed, not just what it fixed (trimming for a length budget can drop an authorisation's conditions).
5. Adding a capability: write the user-facing doc FIRST and check each sentence against the member it describes; state the feature's limits beside it.
6. Then find every doc that asserted its absence: grep the CONCEPT (every file naming the feature) and read the hits, never only the phrases you just edited (an unlock command once left several "no unlock command" claims behind).
7. Keep kinds of evidence apart: a passing unit or adapter test, a metadata check and observed behaviour in a real install are different claims; never collapse them into "it works".

## Never

- Call two observations plus a plausible mechanism settled.
- Treat a reversal said in conversation as a reversal.
- Delete a condition to fit a document budget.

Details: the repo's doc rules (`python tools/doc_index.py --find docs`).
