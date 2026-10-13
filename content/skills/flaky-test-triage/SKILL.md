---
name: flaky-test-triage
description: Use when a load-sensitive or timing-dependent test fails intermittently, or when you must decide whether a change made one better or worse by comparing runs. Covers paired runs and telling a dropped event from a slow one.
---

# Flaky test triage

- **Sequential runs in different windows measure the machine, not the code.** One commit can give 3/3 red in a busy six-minute window and 17/17 green when quiet, with the base beside it 13/14; two runs, each convincing, each wrong. Interleave and pair the runs, same commit, same window.
- **Expiry at the budget and lateness are different failures.** Expiring AT the budget means the event never came; satisfied late means the work was slow. Compare the failing run's duration to the work's nominal cost: a ~34x margin (20 s budget, 0.58 s work) is a dropped event; ~1.3x (400 ms deadline, ~100 ms of polls after Windows' 15.6 ms timer granularity) is genuine slowness. Read the JUnit XML `time=` (or the runner's own timing) for the failing case before choosing which one you are fixing.
- Do not rerun to turn a red green; only a flake on the repo's list (`python tools/doc_index.py --find flakes`) earns one rerun.
