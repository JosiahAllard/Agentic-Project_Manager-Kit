# Flaky tests: the time fence

A flaky test is a test whose result the machine's speed can decide. The kit fences git (docs/test-tools.md "Fake git"), the home folder (docs/test-tools.md "Home fence") and, here, time: a wall-clock budget whose expiry changes an output (a degrade) is the dangerous kind, because a slow machine changes the answer instead of failing loudly. A ceiling that only ends a hang does not need the fence.

The case that prompted it: the agent-start block (`kitlib.agentstart`, default `BUDGET = 1.0` s) printed `NOT CHECKED - the rest: over the 1.0 s budget` on a loaded machine, and `tests/agentview/test_agentview.py` `Start.test_a_dispatched_role_on_claude_shows_the_agent_start_block`, which asserts `checkout  NOT CHECKED`, went red though nothing was wrong.

## How the fence works

- `kitlib/clock.py`: `FENCE_ENV` (`KIT_TIME_FENCE`), `CEILING` (600 s, past `kitlib.tooldeps.selfrun.UNIT_TIMEOUT`), `fenced(environ=None)` and `budget(seconds)`. `budget` returns `seconds`, or `CEILING` in a fenced process: only the value `1` fences. Both read the environment at each call.
- Wiring: `tests/__init__.py` sets the marker unless it is already set. `child_env` in `kitlib/tooldeps/childenv.py` does the same for every unit, because a pool worker replaces its environment for each unit; `tools/kit_tests.py` itself stays unmarked. Production never sets it, and `tests/clock/test_clock.py` `Production` holds that: outside `tests/`, only `kitlib/clock.py`, `tools/kit_tests.py`, `kitlib/tooldeps/childenv.py` and this doc name the marker.
- A degrade budget takes `budget=None` and resolves it with `clock.budget(BUDGET)` at call time (`kitlib.agentstart` `text` and `lines`). A budget passed in is used as is, never fenced.
- Not fenced: a child whose environment a test built without the marker, and a child that is not the kit's Python code. Such a child runs on the real budget and keeps the old risk.
- Under the fence a slow machine makes a fenced test slower, not different, and a real hang still ends, as a unit TIMEOUT.

## Tests

- `tests/clock/test_clock.py`: the test process is fenced and an unmarked one gets the budget; the ceiling is past the unit timeout; a kit child started with the test's environment is fenced and one built without the marker is not; `child_env` marks the units and keeps an outer value; the `Production` scan above.
- `tests/agentview/test_agentview.py` `Start`: the case above runs the hook's default budget with a `repo.classify` stub that takes 1.2 s and asserts `NOT CHECKED`; `test_a_real_run_keeps_the_one_second_budget` pops the marker, blocks the stub on an Event and asserts `over the 1.0 s budget`.
- A file directly in `kitlib/` is a `full` glob in `tests/selection.json`, so a change to `kitlib/clock.py` runs the whole suite.

## Rules

- A budget whose expiry is a normal output (a degrade) goes through `clock.budget` with `budget=None` as its default. A ceiling that only ends a hang does not.
- A test of the over-budget path passes an explicit budget and blocks the work on an Event (as `tests/agentstart/test_agentstart.py` `test_the_budget_cuts_what_is_left` does), or pops the marker.
- A load flake is a test defect, fixed by a seam or a marked tier, never by listing it under "Known flakes" (docs/agent-profile.md). A red `gate.py --full` gets the targeted rerun (docs/gate.md "Targeted rerun").
- A sleep or an elapsed-time assert that flakes gets nothing on its first failure (the builder rule: a one-off flake waits for a second); a lint that counts them against a falling baseline is the follow-up if one does.

## Open

- The Codex hook's `DEADLINE` (`hosts/codex/adapter/host.py`) is a fail-closed safety timeout: still deciding at the deadline is a `deny guard-timeout` (`kitlib/host/hookmain.py`). The fence does not touch it. Fencing it would change what a test sees of a safety check, so it is the owner's decision.
