---
name: branch-review
description: Use as a coordinator before giving any branch its landing verdict. Covers the scope check, when to read the diff yourself and when to dispatch a review, what to look for, sending findings back, the three verdicts, follow-up records, preventing a defect class seen twice, and CI changes that only add strictness.
---

# Coordinator: branch review

No branch reaches the land queue without your verdict. Green proves the project still works, not that the change is right, in scope, or exercised.

1. Gather the brief, the exact diff range, the author's gate report (the final one on that tip, as `<kit>/docs/gate.md` "The final gate" sets it; never bounce a scoped final gate that section allows) and log paths; `python <kit>/tools/review_pack.py -C <worktree> <branch>`.
2. Scope check, yourself: `git diff --stat <merge-base>..<branch>` against the brief. A file outside scope is a finding before you read a line.
3. Small (the profile's **Review** section may set the size) and low risk: read every hunk yourself (docs and additive content: skim). Larger, medium or high risk: dispatch a fresh analyst (the read-only role) with skill `specialty-review`, never the author, its tier by the "Reviewer model" rule in skill coordinator-dispatch, roster.md, after `python <kit>/tools/land.py -C <main checkout> <branch> --check-strict` (read-only; `STRICT` means a strong-tier reviewer and a lone landing); give it the brief, range, gate report and logs. The verdict stays yours; `verdict.py add` refuses a Medium or High-risk one you reviewed yourself (`--reviewer-id` your session, or `--reviewer` the coordinator role), because the auto-mode classifier refuses the submit that follows as self-approval, finally and after the review is done. With `.kit.json` `landing.self_review_max_lines` set, it also refuses your own review of a larger low-risk diff, so a larger diff gets an analyst review. Standing checks: `python <kit>/tools/doc_index.py -C <worktree> --find review`.
4. Look for: what the brief asked and only that; a test that fails without the change, and that it ran; new failure paths handled, not merely unreachable; a changed public interface, wire format, schema or stored data; a check weakened for green (a gate made green by deleting or loosening a test goes back); inferences marked as inferences; a commit trailer that misnames the host or model.
5. Only a flake on the repo's known-flake list gets one rerun; any other failing run is real (the list: `python <kit>/tools/doc_index.py -C <worktree> --find flakes`).
6. Findings go to the author by continuing that agent: numbered, file and symbol, each BLOCKING, FIX-HERE or NOTE (a NOTE is report only, never a record); it fixes and re-runs its gate; you re-review the fix only.
7. Small findings are fixed on this branch, never filed as follow-ups, including incidental defects found there (a tool bug, a wrong doc line, a flake fix); `backlog.py add` refuses a record whose scope overlaps the branch. File a follow-up only for work too large or out of scope, and worth doing. What a record needs: skill builder, step 10; a reviewer's hypothetical stays in the review report; no record for a nitpick or an overengineered fix; a landed-branch review finding is a note unless it cites an observed failure; a guard or landing-tool record cites an honest mistake an agent actually made, never a probe, a reviewer's evasion form or a forging case.
8. Verdict: LAND; LAND WITH FOLLOW-UP (recorded in the plan); SEND BACK. Submit it with the branch, its full tip hash and the reviewed range. A design disagreement goes to the owner, both positions stated fairly.
9. A defect class seen twice is prevented in this order: a guard in code (fail-fast check, test, or a script that does the step); else the reference doc owning the area, the incident in the findings or decisions log; only then one instruction line, a rule not a story, if every agent must act on it.
10. A CI change that only adds strictness (pass-string grep, test step, assertion) for an approved plan item is ordinary work: review it as High risk and land it.

## Never

- Decide from green alone, or review your own implementation.
- Write the repo's instruction files or tracked host config: owner-only; propose. Restate in a profile what the instructions or a reference doc already say.
- Land a check removed, loosened, skipped or made optional without the owner's typed go.

Details: `<kit>/docs/review-pack.md`; the coordinator role file, "The owner".
