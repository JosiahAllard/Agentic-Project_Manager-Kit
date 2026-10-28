---
name: flaky-test-triage
description: Use when a load-sensitive or timing-dependent test fails intermittently, or when you must decide whether a change made one better or worse by comparing runs. Covers paired runs and telling a dropped event from a slow one.
---

# Flaky test triage

1. Read the actual failing lines and the repo's known-flakes list: `python <kit>/tools/doc_index.py -C <worktree> --find flaky`, and the profile's "Known flakes".
2. Read the failing case's own duration (the JUnit XML `time=`, or the runner's timing) before deciding what you are fixing.
3. Expiry AT the budget means the event never came; satisfied late means the work was slow. Compare the duration to the work's nominal cost: a ~34x margin (20 s budget, 0.58 s work) is a dropped event; ~1.3x (400 ms deadline, ~100 ms of polls after Windows' 15.6 ms timer granularity) is genuine slowness.
4. Before changing a timeout, compare deadline, nominal work cost, polling cadence and machine load.
5. Reproduce on the unchanged base too: it tells an existing failure from a new regression. Use an isolated scratch worktree.
6. Comparing branch and base: interleave and pair the runs in the same window. Sequential runs in different windows measure the machine, not the code (one commit gave 3/3 red in a busy six-minute window and 17/17 green when quiet, the base beside it 13/14).
7. Report the exact failure, the timings and sample counts; label any run on a shared machine "shared machine". Heavy repeat runs go under the machine lock (skill machine-lock-run).

## Never

- Rerun to turn a red green: a red `gate.py --full` gets one targeted rerun (`python <kit>/tools/gate.py -C <worktree> --rerun`; skill builder, step 7) and nothing more. FLAKE: report the `backlog.py add` line it prints; the coordinator decides on filing it. REAL, or a test that failed twice: the failure is real and never lands.
- Loosen an assertion or a timeout without the evidence above.

Details: the repo profile, "Known flakes".
