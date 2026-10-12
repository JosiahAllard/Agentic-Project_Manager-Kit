# Agent roster

Read from skill `coordinator-dispatch`, "Which agent, which model". A repo's own
`.claude/coordinator-profile.md` lists the agent types it actually has; use only those.

## Agent types and what each is for

- `pm-kit:worker`: routine coding, low and medium risk; plan-only and sync or prep work (merging main into a branch, rebasing, conflict-free syncs, preparation).
- `pm-kit:opus-worker`: High-risk build and fix work on standard paths (API changes, live fixes, ordinary tool features).
- `pm-kit:strong-worker`: concurrency, networking, simulation or ordering, landing or lock tools, migrations, CI config, known-area crash fixes.
- `pm-kit:deep-worker`: its own launched session for the hardest issues; never an Agent dispatch.
- `pm-kit:reviewer`: read-only review of one branch against its brief. Never the author.
- `pm-kit:lander`: lands one reviewed branch (or a batch) on the coordinator's go. Never edits code.
- `pm-kit:records`: settled facts into docs only (decisions, landing notes, plan ticks).
- `pm-kit:spec-writer`: extends a named spec from the owner's answers. Docs only.
- `pm-kit:researcher`: read-only ideas and prior art; proposes concepts for the owner to pick.
- `pm-kit:test-runner`: runs test tiers in a given worktree; reports PASS/FAIL plus failing lines.
- `pm-kit:tech-artist`, `pm-kit:asset-smith`, `pm-kit:sound-smith`: creative work (see "Creative dispatches" in the skill).
- `pm-kit:consultant`: a long-lived launched session per repository that answers engineering questions.

## Choosing

- Cheap model: landings, records, doc ticks, moves, routine coding. Strong: concurrency,
  networking, migration, security, crash and measurement work, design, specs that read code, any
  change nobody here can observe. Never the weakest where a failure must be judged.
- Plan-only and sync/prep work (merge main into a branch, rebase, conflict-free sync,
  preparation) goes to `pm-kit:worker` (sonnet), whatever the branch's risk.
- `pm-kit:worker` = default coding, low/medium risk (`--find risk`): additive content, UI, pure logic
  with tests, known-cause fixes, behaviour-preserving moves and renames (brief names **Moves and
  renames**; its equivalence check is not optional). Coding ladder by need:
  `pm-kit:worker` (sonnet/medium: Low, Medium) -> `pm-kit:opus-worker` (opus/medium: High-risk build and fix work - API
  changes, live fixes, ordinary tool features; never plan-only or sync/prep) -> `pm-kit:strong-worker` (opus/high: concurrency,
  networking, landing/lock tools, build-cache correctness, codegen design; builds and plans)
  -> `pm-kit:deep-worker` (opus 1M/xhigh, own session: root cause or concurrency work strong-worker
  failed at or plainly cannot do). opus-worker not installed ->
  `pm-kit:worker` + `model: opus`. Never `general-purpose` for code. An escalation = reassign upward
  with its findings, not a failure.
- Model and effort per agent are pinned in each agent file's frontmatter (the kit sets them in its ROSTER.md doc; SKILL.md gives its path).
  A kit agent is dispatched with no `model`. Override only upward for risk, and say so
  in status; never `pm-kit:lander` (haiku as pinned, whatever is being landed), and
  `pm-kit:reviewer` only by the reviewer rule below. **Never `fable`** for any agent, reviews
  included, unless the owner asks on that task: `opus` is the ceiling, `pm-kit:deep-worker` the step up.
- Built-ins (`general-purpose`, `Explore`, `Plan`) pin nothing -> pass `model: sonnet` on every
  dispatch, never `opus`. Work that needs opus is coding or design work: it goes to a typed agent
  on the ladder above. An `Explore` or `Plan` answer that reads thin is asked once more with
  `model: opus`; say so in status.
- **Reviewer model** (this is the rule's one home). Read the groups printed under the `files` line
  of `python tools/review_pack.py <branch>`: `High (N)`, `unlisted (= High) (N)`, `Medium (N)`,
  `Low (N)`; a group with no file is not printed. The first row that fits:
  1. A `High` or `unlisted (= High)` group is printed, or a changed path matches a strict landing
     path (`STRICT_DEFAULTS` in `${CLAUDE_PLUGIN_ROOT}/kitlib/gate/strict_defaults.py`: guard, lock,
     gate, proc and hook code, agent definitions) whatever group it is printed in -> `model: opus`.
  2. Only a `Low` group is printed -> no `model`: sonnet as pinned, never raised.
  3. Otherwise (a `Medium` group, nothing higher) -> no `model`; raise to `opus` only for a risk
     you name in status.

  No review pack in the repo -> the same rows on the risk level the brief states.
- Effort is not settable per dispatch: pick the agent for it. High risk -> `pm-kit:opus-worker`, or
  `pm-kit:strong-worker` for the kinds the ladder names. Only when `pm-kit:strong-worker` has been defeated, or
  plainly will be, on a race or crash root-cause or subtle concurrency (never a build or a
  plan): request a `pm-kit:deep-worker` session; say in status when and why.
- `pm-kit:deep-worker` is never an Agent dispatch: it is its own session - own window, 1M model, 500k
  auto-compact window - for the most difficult issues only. You request it; the PM launches it.
  Write a self-contained brief file, then SendMessage the PM: the brief path, your session name
  as requester, and a slug. The PM runs `launch.py deep` (`--plan` first). The session
  reports to you by SendMessage; review its branch like any other, then tell the PM to stop it
  (`launch.py stop --name deep-<slug>`).
- The repo has a running `pm-kit:consultant` session (`consultant-<repo>` in ListAgents): SendMessage
  engineering questions there before the PM; owner matters still go to the PM, which logs the
  consultant's calls as delegated.
- `pm-kit:researcher` = ideas, modes, mechanics, prior art; read-only. Output = concepts for the owner to pick;
  the chosen one -> `pm-kit:spec-writer` before any builder sees it.
- Pass/fail checks whose log nobody needs -> the cheapest agent type the profile lists; none
  listed -> the worker.
