# Kit tool architecture

The shape every kit tool follows. Change this file only with the owner's approval.

## Layers

| Layer | What | Rule |
|---|---|---|
| `kitlib/` (library) | `git` (repo, worktree, main-checkout detection), `proc` (pid shield, job objects, tree kills), `lock` (machine-lock mechanism, `book()`, `slot()`), `records` (landing lock, gate-OK record), `config` (kit defaults, `.kit.json`, roles, kinds table, heavy programs), and one package per ported tool: `commit`, `machinepaths`, `syncbase`, `reviewpack`, `landed`, `stubs`, `worktree` (new, remove/abandon/check, sweep, sweep-task, Windows holder probes; docs/worktree-remove.md), `preflight`, `board`, `forbidden`, `rulingcites`, `trailerscheck`, `pieces`, `pytests`, `tooldeps`, `testlist`, `roster` (docs/ROSTER.md against agent frontmatter), `verdict`, `docs` (budget, index, codemap; docs/doc-tools.md), `ledger` (rulings ledger, standing digest, opt-in pre-commit check; docs/ledger.md), `githooks` (the pre-commit hook's one dispatch over the kit's checks; docs/ledger.md "Pre-commit check"), `ledgerrefs` (docs/ledger.md "ledger_refs"), `pending` (docs/pending.md), `machinesetup` (switch a machine's Claude folders to the kit; docs/machine-setup.md) | No command lines, no printing to the user, no `sys.exit`. Standard library only. All logic lives here once. |
| Entry points | `gate.py` (gate and land), `machine_lock.py` (every other heavy run, status, admin), `commit.py`, `worktree_remove.py`, `worktree_sweep_task.py`, `lock_release_check.py` (pre-check before the release checkout moves), `landed.py`, `preflight.py`, `board.py`, `sync_base.py`, `review_pack.py`, `new_worktree.py`, `worktree_paths.py`, `checkout_guard.py`, `machine_paths.py`, `forbidden.py`, `ruling_cites.py`, `trailers.py`, `ledger.py`, `pm_data.py` (PM data branch `init` / `--adopt`; docs/pm-data-branch.md "Making the branch"), `pieces.py`, `pytests.py`, `tool_deps.py`, `testlist.py`, `kit_stubs.py`, `agent_roster.py`, `doc_budget.py`, `doc_index.py`, `codemap.py`, `claude/hub/hub_notices.py` (posts the lock's window notices to the Action Hub inbox); `installer/new-repo.ps1` bootstraps a repo; `machine_setup.py` and `installer/machine-setup.ps1` switch a machine to the kit | Parse arguments, call the library, print, exit. Paths and exit codes stay stable; subcommands change only with the owner's approval: `book` became `hold`, and the `-C` hand-off is retired. |
| Guards | `claude/hooks/guard.py` (the one Claude PreToolUse hook, every tool a rule can refuse) and the kit git `pre-push` hook plus the opt-in `pre-commit` ledger check (inert unless `.kit.json` names pre_commit_check) | Read-only checks of real state. They deny with the exact command to use instead. |
| Repo layer | `.kit.json` (data) plus at most one repo hook module the kit calls | Repos hold stubs only, never copies. |

## Contracts

- **Booking.** `book(kind)` returns a slot. `slot()` hands back a launcher, and every heavy child runs through it inside the lock's job object (pinned cores, memory measured, killed as a tree). Acquisition fails closed: there is no unlocked fallback. Lock held means a live run in the store whose process is an ancestor, shown by the `MACHINE_LOCK_RUN=<run>:<pid>:<ctime>` nest token that `nest_check` verifies. No env flag counts as proof.
- **Kinds.** One JSON kinds table in kit config, read by the lock and by gate.py. `gate.py --measure` writes a proposed table; the PM reviews and commits it. Sizes never change automatically. A size is a whole number of cpu slots 1..agent_cap; a run may ask for fewer, never more. A program is heavy because it is on the heavy list, Godot runs included, and its size comes from measurement.
- **Gate-OK record.** gate.py writes `<git common dir>/kit-gate-ok/<sha>.json` for exactly the commit it passed. The pre-push hook refuses a push to main without one, whoever pushes. It is on per repo once `.kit.json` says so, after that repo lands through `gate.py land`.
- **Result record.** A branch gate or a landing writes `<git common dir>/kit-gate-result/<branch stem>.<gate|land>.json`, `running` at the start and the verdict at the end; `gate.py wait-result` waits on it read-only so another session wakes when a run ends (docs/gate.md "Result record").
- **Landing.** `gate.py land <branch> --expect-main <sha> --expect-tip <sha> [--lane]`. It holds the repo's landing lock, merges, gates the merged tree under `book()`, pushes, and verifies. A red merge is undone by default. Landings into one repo take turns; branch gates and different repos run in parallel.
- **Roles.** guard.py tells subagents apart by the PreToolUse `agent_id` field. A session-start self-test alarms if the field disappears. Roles, main-branch names and protected paths come from config.
- **Guard rules.** Raw heavy programs are denied unless the lock is held; they are allowed only as arguments of `gate.py` or `machine_lock.py run`. A subagent's edit in a main checkout is denied. Root searches are denied. Damaging git commands in a main checkout are denied (`checkout`/`switch`, `reset --hard`, `clean`, `stash`, push to main without a record). Bash and PowerShell are tokenized, and git_guard's patterns are the fallback.

## Details

- The ledger path comes only from the user-profile machine config (`ledger_path`; docs/kit-json.md "Machine config keys"), which the installer writes once. Nothing overrides it, not `.kit.json` and not env, and guard.py denies agent edits to it. The kit holds no machine paths.
- `slot()` takes the lock's Win32 mutex on the thread that releases it (the owning thread). It prints nothing; entry points print.
- Tests target the library directly; the entry-point tests cover only arguments and exit codes.
- Tests run on an in-process fake git (kitlib/git/fake), except the classes marked `real_git` (docs/test-tools.md "Fake git").
- Once a repo uses `gate.py land`, the repo's own gate, landing, lock-wrapper, heavy-run and git-guard scripts are deleted (ported first).
- The installer is `new-repo.ps1` plus `kit_stubs.py` in the kit release, with nothing extra.

## Build order

1. **Library first:** `kitlib/lock` and `kitlib/proc`, split out of machine_lock.py plus jobrun/pid_shield, with machine_lock.py as a thin CLI (lock-split agent). Then `kitlib/git`, `records` and `config`.
2. **In parallel on the library:** gate.py land and records (universal-gate agent), and guard.py plus pre-push (shell-guard agent).
3. Then the kinds table, measured.
4. Then each repo switches through its coordinator and turns on the push block.

## Packaging
The kit ships as a Claude Code plugin, which the owner installs once.
- **Hooks:** the coordinator SessionStart hook (claude/hooks/hooks.json); guard.py stays in each account's settings.
- **Bundled:** the agent definitions and slash commands.
- **Skills:** landing, red-first, ledger, backlog, machine-lock, refusal, handoff and action-hub. Profiles and CLAUDE.md become pointers to these skills.
- **Code:** tools and kitlib stay Python. Skills explain them, and enforcement stays in the hooks and the gate.
- **Build order:** after kitlib and guard.

## Scheduler UI
- **API:** kitlib/lock exposes one versioned, transport-neutral API, made of read snapshots, a revision number for changes, and validated owner writes. Every UI uses only this API.
- **Adapters:** these are thin layers over the API, built after the lock lands.
  - A Reservations section of the Action Hub page, migrated once the lock API is ready. How it reaches the lock is an open owner question; the planned 127.0.0.1 web page waits on that answer.
  - A lock MCP server in the plugin. Read tools are open to all. guard.py denies agents the write tools, and in the owner's own session each write asks for their approval.
- **The schedule window** stays: `reserve_ui.pyw` (kit root) (logic in `kitlib/lockui/`) is a tkinter adapter over the same API, next to the hub section.

## What comes from ~/.claude
- **Into the plugin:**
  - agents
  - hooks: the coordinator SessionStart hook; guard.py stays in each account's settings
  - tools: the lock goes into kitlib; the rest become optional packages
  - generic skills: backlog-ops, branch-review, branch-sync, coordinator-dispatch, doc-claims-discipline, flaky-test-triage, land-branch, machine-lock-run, mutation-testing, session-handoff, shell-and-worktree-traps, subagent-dispatch
- **Into the repos' .claude/skills:** a skill tied to one repository's engine, models or worktree tools stays in that repository.
- **Never in the kit:** autoMode, git identity, preferences, memory, the knowledge contents and synced skills.

## Action Hub
- One private artifact page (`claude/hub/action-hub.html`, formerly the Owner Inbox) holds the owner's questions, to-do list and story intake, and later the reservations. Stories go to the chosen repo's coordinator for review before they reach its backlog. The PM posts and reads them through the artifact store; nothing waits on an answer.
- The store schema and the PM's steps are in docs/action-hub.md. The action-hub skill wraps them in the plugin.

## PM session start
- `kitlib/pmstart` builds the checklist that `claude/hooks/pm_session_start.py` injects at PM session start; see docs/pm-start.md.

## Host split
- kitlib stays host-neutral. Host specifics come through two interfaces: `HostProfile` (agent process names, session env var, state dir, worktree and owner-path defaults, lock tool path) and `SessionHost` (session names, live sessions, usage files).
- Guard rules judge a neutral `Action(kind, path, command, role)`; a host adapter builds it from its hook payload and renders the reply.
- `claude/` holds everything Claude-specific: plugin manifest, agents, hooks, guard adapter and self-test, the Claude host profile, the hub page and its artifact writer, and Claude-only tests. CLAUDE.md becomes a pointer to AGENTS.md. The lock actor key `claude` becomes `host`.
- Another runtime adds its own folder (adapter plus profile) beside `claude/`.
- Starts after kit/shell-guard and backlog store step 1 land.
