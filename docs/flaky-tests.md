# Flaky tests: a time fence (design, not built)

A flaky test is a test whose result the machine's speed can decide. The kit already fences two
of the three ways that happens: git (docs/test-tools.md "Fake git": the fake answers in-process,
an audit hook in tests/__init__.py refuses a git start, `@real_git` marks the real tier) and the
home folder ("Home fence"). Time has no fence. This design adds one, in the same shape.

## The case

tests/agentview/test_agentview.py, `Start.test_a_dispatched_role_on_claude_shows_the_agent_start_block`,
asserts `checkout  NOT CHECKED`. It reaches `kitlib.agentstart.text` through the Claude adapter
(hosts/claude/adapter/host.py `start_text`) with the default `BUDGET = 1.0` s
(kitlib/agentstart/__init__.py). On a loaded machine the start block took 1128 ms, so the
output was `NOT CHECKED - the rest: over the 1.0 s budget` and a full gate went red. The test
holds no time token at all: the wall-clock budget is a production default it cannot see.

## Survey of tests/

Pattern counts from a search of `tests/**` on the main branch of 2026-10-19; a match is not a defect, only a place to look.

| Class | Count | Sample | Fenced today |
|---|---|---|---|
| Wall budget whose expiry changes output, reached by a default | 1 degrade: agentstart `BUDGET`. Also the Codex hook's `DEADLINE = 7` (hosts/codex/adapter/host.py; kitlib/host/hookmain.py takes it as `deadline`, the Claude adapter sets none), whose expiry is a fail-closed `deny guard-timeout`, not a degrade | tests/agentview/test_agentview.py:161 | no |
| Upper bound on real elapsed time | 75 in 36 files | tests/tooldeps/test_selfrun.py:326 (`< 4.5` s), tests/bridge/test_codex_delivery.py:254 (`< 4` s) | no |
| `time.sleep` / polling | 166 in 55 files | tests/pytests/test_pytests.py (14), tests/proc/test_job.py (7) | no |
| `timeout=` / `deadline` literals | 443 in 144 files | mostly ceilings that only end a hang | by the unit timeout (300 s) |
| Real subprocess | 290 calls in 117 files | tests/proc/test_spawn_sites.py | git yes: fake git and its audit hook; 67 tier markers (`@real_git`, own-process) in 43 files |
| Ports | 9 binds, all port 0 | tests/proc/test_tcp.py:71 | yes, by convention |
| Directory / dict order | 234 listings in 92 files, many unsorted | tests/lock/test_run.py:166 | in effect: NTFS lists names sorted, dicts keep insertion order |
| Env and temp leakage | 54 raw `os.environ` writes in 24 files (137 `patch.dict`); 235 `mkdtemp` | tests/optional/usage/test_usage_watch.py:138 | yes: the pool worker restores the environment, each unit gets its own TEMP (docs/test-tools.md "kit_tests") |

Reading: the unfenced classes are all time. Of those, a budget whose expiry is a normal,
visible output (a degrade) is the dangerous one, because a slow machine changes the answer
instead of failing loudly. A ceiling (a timeout that only ends a hang) only fails when
something hangs or the machine is many times slower than expected.

## Options

| | A. Virtual clock everywhere | B. Time fence on budgets | C. Ratchet lint |
|---|---|---|---|
| What | `kitlib.clock` with `monotonic`/`sleep`; every caller routed; tests advance a fake clock | `kitlib.clock.budget(seconds)`; fenced in tests it returns a ceiling past the unit timeout | a test counts sleeps and elapsed asserts per test file against a baseline that may only fall; new ones need `@real_time('<why>')` |
| Cost | ~60 lib lines, then about 87 existing ad-hoc `clock=`/`now=` seams in 45 kitlib files, 166 sleeps, 75 asserts | ~25 lib lines, ~3 lines of wiring, ~3 lines per degrade budget (1 today), ~30 test lines | ~60 lines and a baseline of ~90 file entries |
| Catches | sleeps, polls, budgets in-process; not `Thread.join`, `Popen.wait` or a child process | every degrade budget reached by a default, the case above included | new sleeps and elapsed asserts written into a test |
| Misses | anything across a process or a thread wait | sleeps and elapsed ceilings in tests | the case above (no time token in the test) |
| Migration | large, file by file | the 1 budget; nothing else changes | none up front |

## Recommendation: B, the time fence

1. `kitlib/clock.py`: `budget(seconds)` returns `seconds`, or `CEILING` (600 s, past
   `kitlib.tooldeps.selfrun.UNIT_TIMEOUT`) in a fenced process (`KIT_TIME_FENCE=1`).
2. tests/__init__.py sets `KIT_TIME_FENCE=1` beside `KIT_FAKE_GIT`; tools/kit_tests.py
   `child_env` keeps it for the units. Production never sets it.
3. A degrade budget takes `budget=None` and resolves it with `clock.budget(BUDGET)` at call
   time. A test of the over-budget path passes an explicit budget and blocks the work on an
   Event, as tests/agentstart/test_agentstart.py `test_the_budget_cuts_what_is_left` already
   does; an explicit budget is never fenced.
4. Not fenced, as with the home fence (docs/test-tools.md "Home fence"): a child whose
   environment a test built without `KIT_TIME_FENCE`, and a child that is not the kit's
   Python code. Such a child runs on the real budget.

Under the fence a slow machine makes a fenced test slower, not different; a child left
unfenced as in step 4 keeps the old risk. A real hang still ends, as a unit TIMEOUT. The
rule it gives reviewers: a budget whose expiry is a normal output goes through
`clock.budget`; a ceiling does not.

Open question, not part of this recommendation: the Codex hook's `DEADLINE` is a fail-closed
safety timeout (kitlib/host/hookmain.py: still deciding at the deadline is a `deny
guard-timeout`). Fencing it would change what a test sees of a safety check, so it is a
separate decision for the owner.

Not now: A costs most and still misses thread and process waits. C is the follow-up if a
sleep or an elapsed assert flakes for a second time (the builder rule: a one-off flake gets
nothing until it fails twice); `test_selfrun.py:326` (`< 4.5` s) is the likeliest first.

## Fit with the gate, redfirst and the known-flake list

- Gate: no new step. The fence lives in the test process, as the fake git does; a
  `tests/clock` test proves a fenced process gets the ceiling and an explicit budget passes
  through, and that a child started with the test's environment is fenced while one built
  without the marker is not. A file directly in `kitlib/` is a `full` glob in
  tests/selection.json, so the build branch runs the whole suite; later changes there do too.
- Redfirst: the first build's test is the case itself, made to fail at will: the agentview
  start test with a git stub that takes 1.2 s. At the base it reads `over the 1.0 s budget`
  (RED); with the fence it reads `NOT CHECKED` (GREEN), whatever the machine's load.
- Known flakes (docs/agent-profile.md "Known flakes"): the standing rule gives a rerun only
  to a listed flake, and any other failing run is real. The fence keeps the list empty: a
  load flake is a test defect, fixed by a seam or a marked tier, never by listing it. The
  list stays for a cause outside the kit (none today). That section's sentence "A failure is
  real until the same tip passes a rerun" reads as a rerun for every failure; it should say
  a failure is real, with no rerun unless listed.

## First build step

One branch: `kitlib/clock.py` (`budget`, `CEILING`, `fenced`), the two lines of wiring,
`kitlib.agentstart` routed through it, its `tests/clock` test, and the 1.2 s agentview
redfirst test above. The Codex hook `DEADLINE` stays out of it (the open question above).
