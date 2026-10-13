---
name: mutation-testing
description: Use when you write or review a guard/regression test, or build or run a mutation-testing harness (mutate a production line, expect a test to fail). Covers why a guard stays green when it is decorative, stale or never ran, and the harness traps that score a mutant "caught" without testing it.
---

# Guard tests and mutation harnesses

Every guard test must be shown to fail: mutate the line it guards, watch the test fail, restore the line. A guard you cannot make fail is decorative (a class-loader guard once written as a tautology). Do the mutation on a throwaway worktree at the pre-change commit, never in place in a working checkout.

## Writing the guard

- Test input that can silently substitute for the thing under test is the same failure as a test that stays green when that thing is deleted. Make the substitution impossible, or assert which one ran.
- An absence assertion ("X does NOT appear") is satisfied by the feature not existing. Pair every one with a positive case, in the same class, that really produces X.
- Never assert a multi-column row as one string: `'| 0 | -' not in row` passes when a neighbouring column changes. Assert the named cell, split against the printed header.
- "Thread is BLOCKED on the monitor" cannot tell holds-across-the-loop from takes-per-operation when the body re-enters the same synchronised map. Assert the mutation cannot LAND, not that a thread blocks.

## Still green after a mutation: three causes, not two

1. The guard is decorative.
2. The anchor the mutation targets is stale, so the mutation was never applied (one change can stale several anchors at once).
3. The mutation was a no-op: text changed, behaviour did not.

Compile both sources and compare bytecode (or equivalent), then report "N of M mutations actually tested something". A bare "all caught" is not evidence.

## Harness traps (each one scored wrong silently)

- **Wrong checkout.** A harness ran against the main checkout, mutated a file the tests never read, reported green. Assert the file mutated is inside the worktree whose tests run.
- **Stale `.pyc`.** CPython keys `__pycache__` on source mtime truncated to whole seconds plus size; a same-size rewrite within one second reuses the old `.pyc`, so the PREVIOUS version is measured. Delete `__pycache__`, or vary the file size, between mutations (or run with `-B`).
- **Shell and `|`.** A test filter containing `|` handed to a shell is read by cmd.exe as a pipe: no result line, every guard scored untested. Run without a shell (`subprocess.run([...])`, no `shell=True`).
- **Missing result line.** Fail loudly on it; never count it as a pass or a catch.
- **Bare `gradlew.bat`.** `subprocess.run(["cmd","/c","gradlew.bat",...], cwd=worktree)` gives "not recognized", exit 1, every mutant RED with no test failing: a silent all-caught. Pass the absolute `gradlew.bat` path, and read failing test names from `build/test-results/**.xml`, never the exit code alone.
