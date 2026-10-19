# Agent roster

Read from skill `coordinator-dispatch`, "Which role, which tier". A repo's own
coordinator profile lists the roles it actually has; use only those. Roles are named bare; a tier
(light, standard, strong, deep) is turned into a model by the host: the tier table per host is in
`<kit>/docs/ROSTER.md`. "Raise to strong" below means: dispatch with the strong tier's model.

## Roles and what each is for

- weak-worker: small, well-defined, low-risk changes expected under 100k tokens (named edits to named files, doc lines, small additive changes with their test).
- worker: routine coding, low and medium risk; plan-only and sync or prep work (merging main into a branch, rebasing, conflict-free syncs, preparation).
- strong-worker: the default for High-risk build and fix work on standard paths (API changes, live fixes, ordinary tool features, generated-data wiring).
- senior-worker: concurrency, networking, simulation or ordering, landing, gate or lock tools, build-cache and codegen design, migrations, CI config, known-area crash fixes.
- deep-worker: its own launched session for the hardest issues; never a subagent dispatch.
- reviewer: read-only review of one branch against its brief. Never the author.
- lander: optional wrapper that submits one reviewed branch (or a batch) for landing, waits for the result and reports. Never edits code.
- records: settled facts into docs only (decisions, landing notes, plan ticks).
- spec-writer: extends a named spec from the owner's answers. Docs only.
- researcher: read-only ideas and prior art; proposes concepts for the owner to pick.
- test-runner: runs test tiers in a given worktree; reports PASS/FAIL plus failing lines.
- tech-artist, asset-smith, sound-smith: creative work (see "Creative dispatches" in the skill).
- consultant: a long-lived launched session per repository that answers engineering questions.

## Choosing

- Cheap tier: landings, records, doc ticks, moves, routine coding. Strong: concurrency,
  networking, migration, security, crash and measurement work, design, specs that read code, any
  change nobody here can observe. Never the lightest where a failure must be judged (the reviewer, by its rule below, excepted).
- Plan-only and sync/prep work (merge main into a branch, rebase, conflict-free sync,
  preparation) goes to the worker (standard tier), whatever the branch's risk.
- The worker = default coding, low/medium risk (`--find risk`): additive content, UI, pure logic
  with tests, known-cause fixes, behaviour-preserving moves and renames (brief names **Moves and
  renames**; its equivalence check is not optional). Coding ladder by need (roles above):
  weak-worker -> worker -> strong-worker -> senior-worker -> deep-worker (root cause or
  concurrency work senior-worker failed at or plainly cannot do). weak-worker not installed -> the worker.
  strong-worker not installed -> the worker raised to strong. Never the host's general-purpose
  agent for code. An escalation = reassign upward with its findings, not a failure. A
  weak-worker's branch is reviewed like a worker's: the reviewer by the rule below.
- Tier and effort per role are pinned in each role's front matter (`<kit>/docs/ROSTER.md` is rendered from it).
  A kit role is dispatched with no model of its own choosing. Override only upward for risk, and
  say so in status; never the lander (light as pinned, whatever is being landed), and the reviewer
  only by the reviewer rule below. Never a model above the strong tier's for any dispatch, reviews
  included, unless the owner asks on that task: the deep-worker session is the step up.
- The host's built-in agents (a general-purpose agent, an explorer, a planner), where it has
  them, pin nothing -> dispatch each on the standard tier, never strong. Work that needs the
  strong tier is coding or design work: it goes to a role on the ladder above. A built-in
  explorer's or planner's answer that reads thin is asked once more raised to strong; say so in
  status.
- **Reviewer model**. The reviewer is pinned to the light tier
  at high effort; its model follows the tier of the role that built the branch.
  The first row that fits:
  1. Built by strong-worker, senior-worker or deep-worker -> raise to strong.
  2. Built by worker or weak-worker -> no override: light as pinned.
  3. Otherwise -> raise to strong.

  A light reviewer's "blocking" claim is verified before it holds a landing: read the cited code
  path yourself or ask a strong-tier review.
- Effort is not settable per dispatch: pick the role for it. High risk -> strong-worker, or
  senior-worker for the kinds the ladder names. Only when senior-worker has been defeated, or
  plainly will be, on a race or crash root-cause or subtle concurrency (never a build or a
  plan): request a deep-worker session; say in status when and why.
- The deep-worker: you request it; the PM launches it. Write a self-contained brief file, then message the PM: the brief path, your session name
  as requester, and a slug. The PM runs `python <kit>/tools/launch.py deep --plan` first. The
  session reports to you by message; review its branch like any other, then tell the PM to stop
  it (`python <kit>/tools/launch.py stop --name deep-<slug>`).
- The repo has a running consultant session (`consultant-<repo>` in the host's session list):
  message engineering questions there before the PM; owner matters still go to the PM, which logs
  the consultant's calls as delegated.
- The researcher's chosen concept -> the spec-writer before any builder sees it.
- Pass/fail checks whose log nobody needs -> the cheapest role the profile lists; none
  listed -> the worker.
