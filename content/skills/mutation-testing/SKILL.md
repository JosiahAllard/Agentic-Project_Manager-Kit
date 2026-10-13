---
name: mutation-testing
description: Use when you write or review a guard/regression test, or build or run a mutation-testing harness (mutate a production line, expect a test to fail). Covers why a guard stays green when it is decorative, stale or never ran, and the harness traps that score a mutant "caught" without testing it.
---

# Guard tests and mutation harnesses

1. Every guard test is shown to fail: mutate the production line it guards, watch the test fail, restore the line. One you cannot make fail is decorative (a class-loader guard was once a tautology).
2. Mutate in a throwaway worktree at the pre-change commit, never in place in a working checkout, another worker's checkout or the shared kit. Heavy or repeated suites run under the machine lock (skill machine-lock-run).
3. Test input that can silently substitute for the thing under test is the same failure as a test that stays green when it is deleted: make the substitution impossible, or assert which one ran.
4. Pair every absence assertion ("X does NOT appear") with a positive case in the same class that really produces X.
5. Assert the named cell of a table row, split against the printed header; never a multi-column row as one string (`'| 0 | -' not in row` passes when a neighbouring column changes).
6. For a lock, assert the mutation cannot LAND, not that a thread is BLOCKED: blocking cannot tell holds-across-the-loop from takes-per-operation when the body re-enters the same synchronised map.
7. Still green after a mutation has three causes: the guard is decorative; the anchor is stale, so the mutation never applied (one change can stale several anchors); the mutation was a no-op. Compile both sources and compare the bytecode (or equivalent).
8. Report "N of M mutations actually tested something", never a bare "all caught".

Harness traps, each scored wrong silently:
- Wrong checkout: assert the mutated file is inside the worktree whose tests run.
- Stale `.pyc`: CPython keys `__pycache__` on source mtime (whole seconds) plus size, so a same-size rewrite within a second measures the previous version. Delete `__pycache__`, vary the size, or run with `-B`.
- A test filter containing `|` handed to a shell is a pipe to cmd.exe: no result line, every guard untested. Pass argv as a list, no `shell=True`.
- A missing result line fails loudly; it is never a pass or a catch.
- `["cmd","/c","gradlew.bat",...]` with a relative name gives "not recognized", exit 1, every mutant red with no test failing. Use the absolute `gradlew.bat` path and read failing names from `build/test-results/**.xml`, never the exit code alone.

## Never

- Count a compile error, a missing result line or a behaviour-neutral edit as a caught mutant.
- Leave a mutation applied, or mutate a checkout you do not own.

Details: the repo profile, "Build and test" (how red-first is done).
