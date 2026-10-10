# Agent roster

Read from skill `coordinator-dispatch`, "Which agent, which model". A repo's own
`.claude/coordinator-profile.md` lists the agent types it actually has; use only those.

## Agent types and what each is for

- `worker`: routine coding, low and medium risk; plan-only and sync or prep work (merging main into a branch, rebasing, conflict-free syncs, preparation).
- `opus-worker`: High-risk build and fix work on standard paths (API changes, live fixes, ordinary tool features).
- `strong-worker`: concurrency, networking, simulation or ordering, landing or lock tools, migrations, CI config, known-area crash fixes.
- `deep-worker`: its own launched session for the hardest issues; never an Agent dispatch.
- `reviewer`: read-only review of one branch against its brief. Never the author.
- `lander`: lands one reviewed branch (or a batch) on the coordinator's go. Never edits code.
- `records`: settled facts into docs only (decisions, landing notes, plan ticks).
- `spec-writer`: extends a named spec from the owner's answers. Docs only.
- `researcher`: read-only ideas and prior art; proposes concepts for the owner to pick.
- `test-runner`: runs test tiers in a given worktree; reports PASS/FAIL plus failing lines.
- `tech-artist`, `asset-smith`, `sound-smith`: creative work (see "Creative dispatches" in the skill).
- `consultant`: a long-lived launched session per repository that answers engineering questions.

## Choosing

- Cheap model: landings, records, doc ticks, moves, routine coding. Strong: concurrency,
  networking, migration, security, crash and measurement work, design, specs that read code, any
  change nobody here can observe. Never the weakest where a failure must be judged.
- Plan-only and sync/prep work (merge main into a branch, rebase, conflict-free sync,
  preparation) goes to `worker` (sonnet), whatever the branch's risk.
- `worker` = default coding, low/medium risk (`--find risk`): additive content, UI, pure logic
  with tests, known-cause fixes, behaviour-preserving moves and renames (brief names **Moves and
  renames**; its equivalence check is not optional). Coding ladder by need:
  `worker` (sonnet/medium: Low, Medium) -> `opus-worker` (opus/medium: High-risk build and fix work - API
  changes, live fixes, ordinary tool features; never plan-only or sync/prep) -> `strong-worker` (opus/high: concurrency,
  networking, landing/lock tools, build-cache correctness, codegen design; builds and plans)
  -> `deep-worker` (opus 1M/xhigh, own session: root cause or concurrency work strong-worker
  failed at or plainly cannot do). opus-worker not installed ->
  `worker` + `model: opus`. Never `general-purpose` for code. An escalation = reassign upward
  with its findings, not a failure.
- Model and effort per agent are pinned in each agent file's frontmatter (the kit sets them in its `${CLAUDE_PLUGIN_ROOT}/docs/ROSTER.md`).
  Built-ins pin nothing -> pass `model` every dispatch. Override only upward for risk, and say so
  in status. **Never `fable`** for any agent, reviews included, unless the owner asks on that task:
  `opus` is the ceiling, `deep-worker` the step up.
- Effort is not settable per dispatch: pick the agent for it. High risk -> `opus-worker`, or
  `strong-worker` for the kinds the ladder names. Only when `strong-worker` has been defeated, or
  plainly will be, on a race or crash root-cause or subtle concurrency (never a build or a
  plan): request a `deep-worker` session; say in status when and why.
- `deep-worker` is never an Agent dispatch: it is its own session - own window, 1M model, 500k
  auto-compact window - for the most difficult issues only. You request it; the PM launches it.
  Write a self-contained brief file, then SendMessage the PM: the brief path, your session name
  as requester, and a slug. The PM runs `launch.py deep` (`--plan` first). The session
  reports to you by SendMessage; review its branch like any other, then tell the PM to stop it
  (`launch.py stop --name deep-<slug>`).
- The repo has a running `consultant` session (`consultant-<repo>` in ListAgents): SendMessage
  engineering questions there before the PM; owner matters still go to the PM, which logs the
  consultant's calls as delegated.
- `researcher` = ideas, modes, mechanics, prior art; read-only. Output = concepts for the owner to pick;
  the chosen one -> `spec-writer` before any builder sees it.
- Pass/fail checks whose log nobody needs -> the cheapest agent type the profile lists; none
  listed -> the worker.
