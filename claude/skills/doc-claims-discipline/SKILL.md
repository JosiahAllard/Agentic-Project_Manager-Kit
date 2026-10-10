---
name: doc-claims-discipline
description: Use when you record a finding or conclusion, withdraw one, trim or delete lines from a doc, or add a capability that earlier docs may say does not exist. Covers evidence-labelling, reversals living in the file, what a deletion silently removes, and stale absence claims.
---

# Findings and doc edits

- **Two observations plus a plausible mechanism is not settled.** Write the evidence next to the claim and let observed / measured / inferred / untested read off it; do not pick the label first. A claim that names its evidence ("one timestamp, nothing to compare against") cannot be upgraded in a retelling without deleting the evidence that contradicts it. A finding that holds has a control, one changed variable, and a prediction written down before the run.
- **A reversal said in conversation is not a reversal.** When a finding falls, edit the file that claims it; a claim left filed gets repeated as fact, even by whoever withdrew it.
- **A deletion removes every rule in the bullet it edits.** Trimming a decision line for a length budget can drop an authorisation's conditions. Say what a deletion removed, not just what it fixed.
- **Adding a capability falsifies every doc that asserted its absence**, and grep misses them because you grep the strings you already fixed (an unlock command can leave several "no unlock command" claims behind). Read prose against code: write the user-facing doc FIRST and check each sentence against the member it describes. When you must grep, grep the CONCEPT (every file naming the feature) and read the hits, never the phrases just edited.
