---
name: branch-review
description: Use as a coordinator before giving any branch its landing verdict. Covers the scope check, when to read the diff yourself and when to dispatch a reviewer, what to look for, sending findings back, the three verdicts, follow-up records, preventing a defect class seen twice, and CI changes that only add strictness.
---

# Coordinator: branch review

No branch reaches the lander without your verdict. Green proves the project still
works, not that the change is right, in scope, or exercised.

1. Scope check, yourself: `git diff --stat` branch vs merge base against the brief. Files outside
   scope = a finding before you read a line.
2. Read every hunk yourself only when the diff is small (~150 lines) and low/medium risk; docs and
   additive content: skim. Larger or high-risk -> `pm-kit:reviewer` (fresh, read-only, never the author;
   its model by the "Reviewer model" rule in skill `coordinator-dispatch`, roster.md, nowhere
   else), given the brief, diff range, the author's gate report and log paths. You read findings; the verdict stays yours. Standing checks:
   `--find review`.
3. Look for: does what the brief asked and only that; a test that would fail without the change,
   and did it run; new failure paths handled or merely unreachable; public interface, wire format,
   schema or stored data changed; a check weakened for green (a gate made green by deleting or
   loosening a test goes back); inferences marked as inferences. A failure is real unless it is
   on the known-flakes list (`--find flakes`, kept current).
4. Findings -> the author, by continuing that agent: numbered, file + symbol. It fixes and re-runs
   its gate; you re-review the fix only.
5. Small findings are fixed on this branch before it lands, never filed as follow-up records; file
   a follow-up only for work too large or out of scope, and worth doing. The same bar holds for
   every agent's own findings: a minor one folds into its story, not a new record. This covers
   incidental defects found on the branch (a tool bug, a wrong doc line, a flake fix), not only
   the branch's own story; `backlog.py add` refuses a record whose scope overlaps the finding
   branch. Verdicts: land;
   land with a follow-up recorded in the plan; send back. Design disagreement -> the owner, both
   positions stated fairly.
6. A defect class seen twice is prevented in this order: **a guard in code** (fail-fast check, test,
   or a script that does the step); **else the reference doc owning the area**, the incident going
   to the findings or decisions log, never an instruction file; **only then an instruction line** -
   one line, a rule not a story, only if every agent must act on it. CLAUDE.md and `.claude/` are
   owner-only: propose, never write. One home per rule: never restate in a profile what CLAUDE.md or
   the reference doc says.
7. A CI change that only adds strictness (pass-string grep, test step, assertion) for an approved
   plan item = ordinary work: review it like any High-risk file and land it. Removing, loosening,
   skipping or making optional an existing check still needs the owner's typed go (coordinator.md, The owner).
