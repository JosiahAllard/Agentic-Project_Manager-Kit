# Agent review, 2026-10-11

A review of the 15 agent definitions in `claude/agents/`, with usage, refusal and miss-history data behind it. The A/B measurements are in [model-ab.md](model-ab.md); this page is everything else from the same review.

Status: findings only. Nothing here changed a definition. Every recommendation is **UNDECIDED**: the owner has ruled on none of them. Most edit `claude/agents/`, which only the owner may change, so each of those needs the owner to apply it (C7 edits docs/ROSTER.md). "Owner decision" means the content is the owner's call (a model, tool list, safety rule, roles file, setting, or removing or loosening a check). "Engineering call" means a wording fix that adds strictness or removes duplication without changing behaviour.

Evidence labels:
- **measured** (M): from a script or log in the source notes. Not re-run for this page.
- **checked** (C): read at the file, in one read or grep, during the review.
- **inferred** (I): a lead, not a fact.

Sources, named by what they are: the usage refresh (window 2026-10-08 14:08 to 2026-10-11 18:17 UTC, 1,157 subagent runs), the definitions review (all 15 files), the miss-history study (two rounds), the replay scoring notes, a gate-speed study in another repository, and the refusal log. Single-run figures are marked single-run.

## Summary

- The main cost is refusals, not the wrong model. 221 of 1,157 runs (19%) hit at least one block. 94% of block events are the kit's own hooks, not the safety classifier (M).
- No definition warns about the patterns behind most blocks (C): none mentions `python -c`, a literal-path write rule, or a sleep ban other than the lander's.
- The model in a definition is often not the model that runs (M): the lander is defined Haiku and 168 of 200 ran on Sonnet; the reviewer is defined Sonnet and 137 of 295 ran on Opus. Every mismatch matches a dispatch override. Editing a tier line does little until the dispatch habit changes.
- The tier evidence is thin: one run per cell and few cases. Real review history cannot separate the reviewer tiers.
- The money is in the two Opus coder types: 65% of weighted usage. Lander on Sonnet is 1.7%, reviewer on Opus is 6.2%.
- The coordinator replay is in [model-ab.md](model-ab.md) (single run per cell).

## Usage and dispatch overrides (M)

Window: 3.17 days, 1,157 runs, 4,237M tokens, 705M weighted units (wu = input + 1.25 cache write + 0.1 cache read + 5 output). No price table was applied. Served model family by wu: Opus 543M (77%), Sonnet 134M, Haiku 28M. Every served id was `claude-<family>-5-5`.

| Agent | Runs | Defined | Served (runs) | Tokens, median |
|---|---|---|---|---|
| opus-worker | 135 | opus, medium | opus 128, haiku 7 | 7.57M |
| strong-worker | 95 | opus, high | opus 92, haiku 3 | 8.53M |
| worker | 210 | sonnet, medium | sonnet 191, haiku 14, opus 5 | 1.46M |
| reviewer | 295 | sonnet, high | sonnet 150, opus 137, haiku 8 | 0.56M |
| lander | 200 | haiku, medium | sonnet 168, haiku 32 | 0.25M |
| records | 99 | sonnet, low | sonnet 91, haiku 8 | 0.18M |
| researcher | 7 | sonnet, high | sonnet 6, opus 1 | 1.27M |
| test-runner | 8 | haiku, no effort key | sonnet 4, haiku 4 | 0.13M |
| spec-writer | 2 | sonnet, high | sonnet 1, opus 1 | 4.1M |
| general-purpose, Explore, Plan, claude-code-guide | 60, 14, 5, 27 | no definition here | general-purpose: opus 24, sonnet 19, haiku 17 | |
| asset-smith, tech-artist, sound-smith | 0 | | | |
| deep-worker, consultant, coordinator | not measured | | run as sessions | |

- Every served model that differs from the definition matches the dispatch `model` override recorded in the run's meta file. The harness substituted nothing. Runs with no override all served the definition's model (M).
- Lander: 150 of the 168 Sonnet runs carry an explicit `model: sonnet`. The 24 Haiku runs with no override began 2026-10-10, when the definition moved to Haiku. Production Haiku medium: 20 landed, 2 not (24 runs). Sonnet medium: 35 landed, 10 not (50 runs). The outcome is a text match (H) and the counts are too small to rank.
- Reviewer: 46% ran on Opus (kit 94 of 133, a second repository 30 of 50, a third 13 of 111). Verdicts were not scored in this window, so there is no evidence that Opus catches more.
- Haiku and downward overrides (worker 14, records 8) were mostly one Haiku A/B session of 51 runs (139M tokens).
- Where overrides come from (C): docs/ROSTER.md:18 allows overriding "only upward". The review notes also cite the dispatch skill's roster file for passing `model` on every dispatch; that file was not re-read for this page. A Sonnet lander over a Haiku default is an upward override, so it is allowed. Why dispatchers pass it (habit, or an older brief template) is not shown in the notes (I).
- Weighted units, biggest lines: opus-worker on Opus 233M, strong-worker on Opus 223M, worker on Sonnet 85M, reviewer on Opus 44M, general-purpose on Opus 34M. Reviewer on Sonnet 22M, lander on Sonnet 12M, records on Sonnet 6M.
- Since the previous review (per-day rates): runs up 35%, tokens flat. Lander median 0.44M to 0.25M. Reviewer Opus share 74% to 46%. General-purpose Opus share 73% to 40%. Strong-worker median 16.9M to 8.5M. Opus-worker plan-shaped share 45% to 33% (direction matches the advice; size within the noise of the shape heuristic, I).
- Runs ended without a usable report (H): opus-worker 8, strong-worker 5, worker 8. No capability escalation was found for worker: 0 confirmed of 19 candidate pairs (pairing by shared record id, H).

### Plan-shaped work on the Opus coders (M, shape tag H)

44 plan-shaped opus-worker runs hold 720M tokens (47% of the type). 39 strong-worker runs hold 387M (26%). The tag uses the first dispatch only, and the three biggest "plan" runs went on to build, so these shares are upper bounds. If plan-shaped opus-worker runs cost what worker plan runs do, about 630M tokens would be saved. That is a ceiling, not a forecast. Strong-worker plans are within its charter and nobody tested a cheaper tier on them.

## Refusals (M for counts, H for class)

328 block events in 221 runs, by a strict start-of-message match. By source: kit hooks 307 (94%), safety classifier 21 (6%). By repository: kit 175, a second repository 104, a third 45, a fourth 4.

| Agent | Runs (refused) | Events | Main-checkout write | Unbounded wait | Unplaceable command | Wrapped gate or pipe | Other | Classifier |
|---|---|---|---|---|---|---|---|---|
| worker | 210 (52) | 71 | 29 | 12 | 6 | 10 | 11 | 3 |
| opus-worker | 135 (34) | 65 | 32 | 2 | 8 | 7 | 13 | 3 |
| strong-worker | 95 (29) | 47 | 28 | 3 | 7 | 1 | 4 | 4 |
| lander | 200 (42) | 45 | 3 | 30 | 3 | 1 | 3 | 5 |
| reviewer | 295 (31) | 39 | 27 | 0 | 4 | 0 | 4 | 3 |
| general-purpose | 60 (15) | 34 | 22 | 1 | 5 | 0 | 5 | 1 |
| records | 99 (12) | 18 | 4 | 4 | 4 | 3 | 2 | 1 |
| Explore, Plan, test-runner | 27 (6) | 9 | 2 | 0 | 2 | 0 | 4 | 1 |
| all | 1,157 (221) | 328 | 147 | 52 | 39 | 22 | 46 | 21 |

- The class columns are a text match (H). The Classifier column is a count inside the event total.
- Main-checkout writes are 45% of all events. The cause is probably workers running `python -` or `New-Item` from the wrong working directory (I).
- Most lander blocks (30 of 45) are the unbounded-wait rule (sleep or until loops in the foreground), even though lander.md:68-70 already forbids polling.
- Classifier denials by label: Auto-Mode Bypass 7, Modify Shared Resources 5, Irreversible Local Destruction 3, and one each of CI Bypass, Security Test Removal, Credential Exploration, PII Data Handling, Interfere With Workloads.
- The refusal log (185 rows for the same days, written by the project manager and coordinators, so it counts reported refusals only) agrees on the main cause: the kit's own hooks. Rows per day: 11, 6, 78, 90. The log grew sharply from 2026-10-10, when the kit hooks and the plugin dispatch names came in.
- Stop-like reports in the first 250 characters (H, upper bound): worker 57 of 210, opus-worker 36, strong-worker 33, lander 42, records 47, reviewer 29. For worker, 37 of the 57 were refusal or hook stops, 11 gate red, 4 parked for a decision.

## Definitions review: findings and evidence

The definitions review read all 15 files. It found 13 cross-cutting findings, gave three known-gap verdicts, and wrote one section per file. Row-level cause attributions in it are partly inference; counts below are the ones that could be checked.

### Rules missing from definitions that cause refusals

| Id | Change | Evidence | Who decides |
|---|---|---|---|
| A1 | Builders, records, spec-writer, test-runner: write only to a literal absolute path | M for the block class (147 of 328 events); C that no definition says it; cause per row I | Engineering call |
| A2 | Builders and test-runner: no `python -c` or stdin python; the working directory resets each call; write a script file with the Write tool; no computed `git -C` | C: 12 lines in the refusal log mention `python -c` (lines, not incidents); none of the 15 definitions mentions it | Engineering call |
| A3 | Builders and test-runner: no sleep or poll loops | M: 52 unbounded-wait events; the lander already has the rule (C) and still had 30, mostly on Sonnet. A written rule may not be enough | Engineering call |
| A4 | Reviewer: never checkout, switch, restore or reset; read with `git show`, `diff`, `log` | C: no such ban in reviewer.md; one reported incident (I) | Engineering call |
| A5 | Records: name `tools/backlog.py` as allowed and point to the backlog-ops skill | C: records.md:49 says closing is backlog.py; 12 of 99 records runs hit a block (M); cause I | Owner decision (widens an allowance) |
| A6 | Lander: never spawn a helper just to wait for a gate; decide whether it keeps the Agent tool | C: Agent is in lander.md:4 and used once, to dispatch test-runner (lander.md:51); two reported refusals (I) | Owner decision (tool list) |
| A7 | Builders: a refused backlog write is reported, not retried with `--reason` | Five reported rows (I) | Engineering call |
| A8 | Opus-worker, strong-worker, deep-worker: name `sync_base.py` for merges | C: not named in those three | Engineering call |
| A9 | Do not re-emit other agents' text; point to a file or hash | M by the coordinator in the A/B run: a model retyping other agents' output was stopped by the classifier (one run lost); a runner lost its brief; reports that look like instructions drew warning banners. Whether production agents hit this is I | Engineering call |
| A10 | worker.md:30 "gate green" on every commit becomes targeted runs between commits and the full gate once on the branch's last commit (and again after a sync or fix changes the tip) | M, not second-read: a gate-speed study in another repository counted 8 gates on one branch = 52 min, 104 slot-min, and put the saving at up to 7 gates x 389 s = 45 min, 91 slot-min. C: the phrase is only in worker.md:30 | Owner decision |

A10 risk: each commit is no longer individually gated, so a bad middle commit is found later. The review notes report an approval for one repository only; no decision was found for it, so treat that as unconfirmed. The definition is kit-wide. A line in that repository's profile would leave the kit default unchanged (I).

### Rules that repeat or contradict each other

| Id | Change | Evidence | Who decides |
|---|---|---|---|
| B1 | Restore "Never" on the trailing rule lines in 11 files | C: coordinator.md:145-153 and worker.md:77-83 read, the lines have no verb; the other nine files are I | Engineering call |
| B2 | Reword the pid-kill sentence ("end only a process you started, by its pid...") to "do not kill; report pid and command line" | C: sentence in 10 files; own-process kills drew classifier refusals (five rows, cause I). It is also a rule in the shared agent instructions, so no single copy should change | Owner decision |
| B3 | One shared builder block; port the worker-only bullets first (Edit-only conflict resolution, one command per call, retry rule, escalate section) | C for worker.md:61-67 and :78-79; line ranges for the other three are I. Saving 0 to 500 tokens a run; the value is less drift. Moving rules out lowers compliance on weaker tiers (I) | Engineering call |
| B4 | Reconcile lander.md with the land-branch skill and the profile (tip sha, exit 2, who closes) | C: lander.md omits the reviewed tip sha from its description and inputs, but step 5 runs `--expect-tip`. Do not shorten lander.md without re-running the Haiku test: lines 10-11 and 40-44 are the precedence rule the lander A/B tested | Engineering call; owner if the model changes |
| B5 | One rule for who closes a record | Three sources, two answers (I; not re-read) | Owner decision |
| B6 | Fix stale names: `new_phase` (coordinator.md:157, C: no such tool), a conventions digest in consultant.md (C: none in the kit, one glob), the refusal-logging line (coordinator.md:158) | C / I as marked | Engineering call |
| B7 | Merge the duplicated art-agent text (about 480 lines across asset-smith, tech-artist, sound-smith) and add one "free the GPU" line each | M: 0 runs in both windows; the kit profile marks art not applicable | Engineering call; owner to retire |
| B8, B9 | Repo-tools block copied to about 14 files; doc-writing block duplicated in records and spec-writer | Negligible per run | Engineering; low value |

Dropped on check: the definitions review said the state note is not git-ignored in the kit. The kit's ignore file has `docs/ops/*.local.md` (line 5), which covers it (C). No change recommended.

### Model, effort and tool-list fit

| Id | Finding | Evidence | Who decides |
|---|---|---|---|
| C1 | Decide where tier overrides come from before editing any tier line. Either record a reason per dispatch, or list the owner's decided default in the tier table and treat overrides as exceptions the coordinator says aloud | M for counts; source I | Owner decision |
| C2 | Lander: keep haiku, medium. No definition change; the gap is in dispatch. Even a full switch saves little (1.7% of wu) | M, computed | None needed |
| C3 | Reviewer: reviewer.md says Sonnet default and the caller may raise to Opus (I, not reopened). The owner's split (decided 2026-10-10; see model-ab.md) is not in the file or roster. Do not edit the tier text until the split is built | Computed: Opus reviews 44M of 705M wu (6.2%) | Owner decision when built |
| C4 | Plan-shaped work on the Opus coders: fix the wording first (D1, D2); any tier move for plans should be a trial, not a switch | M, upper bound (see above) | Owner for any tier move |
| C5 | Trim unused tools: AskUserQuestion and Artifact on the coordinator (C: AskUserQuestion appears only on the tools line), Agent and EnterWorktree on the art trio and test-runner, Write without a path limit on the researcher | C for coordinator; I for the rest | Owner decision |
| C6 | Deep-worker auto-compact window: definition says 500k, the earlier review saw about 366k. Deep-worker has no subagent runs now | I, not re-checked | Owner decision |
| C7 | Test-runner has no effort key. Intentional; add a one-line note in docs/ROSTER.md | C | Engineering call |
| C8 | No tier change recommended for coordinator, consultant, deep-worker or builders | M: no capability escalations found | None |
| C9 | Haiku coding: no recommendation. See the Haiku coding section of [model-ab.md](model-ab.md) | | Owner decision, untouched |

### Descriptions that cause wrong routing

| Id | Finding | Evidence | Who decides |
|---|---|---|---|
| D1 | Worker's text can send networking work to the wrong tier: it names opus-worker, strong-worker or a deep-worker session, while opus-worker excludes networking and concurrency and sends them to strong-worker | C, from the registered descriptions | Engineering call |
| D2 | "Plan" means two things: plan-only (a task list, no code) goes to worker; strong-worker "builds and plans (designs) included" | C. Harm is I | Engineering call |
| D3 | The registered coordinator description says it "serialises landings"; coordinator.md:3 says it lands every LAND verdict as soon as the lane is free. The plugin copy in use may be older than the file | C. Check that the shipped plugin matches the repo | Engineering call to check |
| D4 | Coordinator has no fallback if dispatched as a subagent (deep-worker and consultant do) | I | Engineering call |
| D5 | Lander description should list the reviewed tip sha; reviewer description should list the gate report and log paths it needs | C for lander (see B4); I for reviewer | Engineering |
| F1 | Finding only: the kit has no `.claude/coordinator-profile.md`, and coordinator.md:3, :12 and :33 require one | C | Owner only |

Known-gap verdicts: the three art agents have no GPU-free text (confirmed; code frees it at the end of each generating command, with edge cases for killed or nested runs, I); the deep-worker 500k window against the earlier 350k figure is confirmed unacted; the test-runner effort key is confirmed intentional.

### Per-definition table (all 15)

"Ran" is the served-model count in the window (M). Settings are from each file's frontmatter.

| File | Defined | Ran | Top issue | Recommended change |
|---|---|---|---|---|
| asset-smith.md | opus, high | 0 runs | 159 lines shared with the other art agents; unused tools; no GPU-free line | Trim tools (owner); share blocks and add one free line (engineering) |
| coordinator.md | opus, medium | main session, not measured | Missing profile (F1); stale `new_phase`; bare Never lines; unused AskUserQuestion; no subagent fallback | B1, B6, C5, D3, D4 |
| consultant.md | opus, high | session, not measured | Names a conventions digest not found in the kit | Verify or drop the line |
| deep-worker.md | opus, xhigh | own sessions, not measured | Missing A1 to A3 and A8; 500k window open | A1 to A3, A8 (engineering); window (owner) |
| lander.md | haiku, medium | sonnet 168, haiku 32 of 200 | Tip sha missing from description and inputs; helper-agent risk; dispatch overrides | B4, A6, C1 |
| opus-worker.md | opus, medium | opus 128, haiku 7 of 135 | Routing text against worker and strong-worker; missing A1 to A3, A8 | D1, D2, A1 to A3, A8 |
| records.md | sonnet, low | sonnet 91, haiku 8 of 99 | backlog.py not named as allowed; 12 of 99 runs hit a block | A5 (owner), A1 |
| researcher.md | sonnet, high | sonnet 6, opus 1 of 7 | Write without a path limit (I) | Owner: add "scratch folder only" or drop Write |
| reviewer.md | sonnet, high | sonnet 150, opus 137, haiku 8 of 295 | No checkout ban; tier text will go stale when the split lands | A4; C3 (owner) |
| sound-smith.md | sonnet, medium | 0 runs | Largest file (168 lines); duplicated blocks; unused tools | B7, C5, one GPU-free line |
| spec-writer.md | sonnet, high | sonnet 1, opus 1 of 2 | Duplicated doc-writing block; no literal-path rule | B9, A1 |
| strong-worker.md | opus, high | opus 92, haiku 3 of 95 | "plans" wording; missing A1 to A3, A8 | D2, A1 to A3, A8 |
| tech-artist.md | opus, high | 0 runs | Duplicated blocks; orphan-sweep text conflicts with B2 | B7, B2, C5 |
| test-runner.md | haiku, no key | sonnet 4, haiku 4 of 8 | No wait-loop ban; kit profile lacks tier names (I); EnterWorktree | A3, A1, A2; C5 (owner) |
| worker.md | sonnet, medium | sonnet 191, haiku 14, opus 5 of 210 | Description sends networking to the wrong tier; holds the only copy of three rules; 52 of 210 runs hit a block | D1, B3, A1 to A3, A10 (line 30) |

### Ranked by expected value

Order (value = refusals or tokens avoided, weighted by how well the evidence holds): A10 (largest single figure, but loses per-commit proof), A1, A2, C1, A3, A6, B1, D1 and D2, A5, B2, A4, B4, A7, A8, A9, B3, B6, C5, B7, B5, F1. The coordinator replay findings are in [model-ab.md](model-ab.md).

## Thin evidence behind standing decisions

- **Lander on Haiku** rests on 2 cases (a clean merge, and a DECISIONS.md conflict), 1 run per cell, over two rounds. The definition was edited after round 1 and re-tested on the same two cases, so round 2 measures fit to those cases (I). The Sonnet "miss" is Sonnet following the brief over the definition's STOP rule: a precedence test, not a capability test. The 48-run lander cell used effort low, a different setup from the medium decision.
- **Reviewer split** rests on 7, 8 and 9 of 10 planted bugs for Sonnet high, Haiku high and Opus, over 8 diffs, one review per model per diff. The A/B page says 1 to 2 catch gaps are noise. Every arm missed a silent data loss.
- This is a record of how thin they are. It is not a request to re-run them.

What a firmer test would cost (estimate from the per-run figures in model-ab.md; thinking tokens are hidden, so costs are understated):
- Lander: 8 cases, 3 arms, 5 repeats = 120 runs, about 16M raw tokens. Sonnet is most of the cost. It would not fix the case where the definition and the brief disagree.
- Reviewer: 20 diffs with 30 or more planted bugs, 3 repeats, 3 arms = 180 reviews, about 141M raw tokens, about 8 hours in series. The Opus arm is about two thirds of the price-weighted cost.
- Both would test the wrong thing in part: planted bugs are small and local; the real misses sat outside the diff.
- Where the money is: the two Opus coder types (65%). Extra measurement belongs there, if anywhere.

## Reviewer miss history (measured unless marked)

- Round 1 found 4 traceable post-landing escapes: 2 true detection misses and 2 defects the reviewer named but rated non-blocking. Round 2 weakened this: case 1's blame points to the squashed first commit and a direct commit, not the reviewed branch (so unproven), and case 3 is probably an in-branch catch.
- Kit denominator: 109 first-parent merges; 84 with a reviewer run found (51 Opus only, 27 Sonnet only, 6 both); 25 with none found. The 25 is an upper bound because the join missed at least one reviewed branch.
- Of 27 fix-worded commits blamed, 0 were clean post-landing misses (16 in-branch review fixes, 8 on the squashed first commit, 3 interactions).
- Result: real history cannot separate the tiers. It does not contradict the planted-bug A/B and does not support it either. The two round-1 misses were consequences outside the diff (a changed root read by a stub tool; a new tool's path text tripping a repo-wide lint). The planted-bug A/B does not test that (I).
- Not done: full blame of 430 commits (about an hour), the join for another repository (174 of 964 matched), the check of case 3.

## What could not be measured

- Main sessions (coordinator, project manager, consultant) and deep-worker sessions: not in the usage data. Compaction, state-note and consultant recommendations are unmeasured.
- Per-token prices for the usage window: weighted units only.
- Review verdict quality in the new window, and whether stop-like runs were re-dispatched except by shared record id.
- Served model id per run in the older A/B tables: not recorded (see model-ab.md).
- Any per-tier reviewer miss rate: zero certain misses in 84 reviewed branches. That is a floor, not a rate.
- Whether refusals would drop after the rule additions.
- Haiku sync and Haiku coding at larger samples; the Haiku helper cost measure (not started).
- The coordinator's tier beyond one file-based run per cell: no live session, tools, long context or compaction.
- Which dispatch habit makes the lander go to Sonnet.
- The full text of the earlier prompt audit and several skills were not read by the definitions review.

## Open A/B work

Not started: reviewer split build and the one-month re-check (owner decision, 2026-10-10: the item is held until go-live and is the first after it; read on 2026-10-11 from the project manager's backlog store, it is still held, and the kit's own store has no such record), the Haiku helper cost measure, a live coordinator pilot (the owner has not decided), an opus-worker trial for plan and sync first dispatches (I). Sync on Haiku: decision open.
