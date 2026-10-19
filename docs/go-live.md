# Go-live list

What remains to switch on: each switch still to make, each decision still open and each fix still to land.

## Switches

- Hooks: changes to hosts/claude/shell/hooks/ or kitlib/pmstart, kitlib/coordinatorstart (all accounts load them from this checkout).
- Action Hub site: the claude.ai artifact hub is retired and its page deleted. The owner goes live in this order. (1) Trial first, with no `hub` key: `tools/hub.py serve --store <scratch folder>` and `hub.py migrate --from <old hub data folder> --store <scratch folder>`; a trial never writes the `hub` key. (2) Write the `hub` key in the machine config: `store` an absolute path to a new, empty folder (or leave it unset for `hub/` in the PM data worktree; docs/pm-data-branch.md), plus any `port` change. (3) Stop any server, then `hub.py migrate --from <old hub data folder> --dry-run`, read it, and run it again without `--dry-run`; it is create-only and a second run prints `nothing changed` (docs/action-hub.md "Data from the old hub"). (4) Start `tools/hub.py serve` and check `hub.py status`. (5) Drop `pm_start.hub_url`, `hub_urls` and `hub_source` from the machine config (the checklist no longer reads them).
- One hook entry: every hook runs `boot/hook.py <host> <event> [<part>]`. Owner action, once on each machine: re-run `tools/machine_setup.py` (dry run, then `--apply`, in the mode the machine was set up with: `--old-kit DIR`, or the default mode with `--pm-settings FILE`). It re-points each settings hook that names a retired script to the entry: `guard.py` to `boot/hook.py claude PreToolUse`, `pm_session_start.py` to `boot/hook.py claude SessionStart pm`, `coordinator_session_start.py` to `boot/hook.py claude SessionStart coordinator` (docs/machine-setup.md "Moved hook paths"). Until it runs, every guarded tool call (Bash, PowerShell, Edit, Write, MultiEdit, NotebookEdit, `mcp__*`) in every Claude session of an account whose guard entry names a removed script is blocked with Python's `can't open file '<kit>/.../guard.py'`, the session that landed the change included. So run it from a terminal, not from a Claude session. Then restart the Claude sessions that were open (a session reads its hooks when it starts) and refresh the Claude and Codex plugins (their hooks.json changed; the plugins' own hooks need no machine setup). Under `--old-kit`, a PM repo's own settings file that runs `pm_session_start.py` is edited by hand (docs/machine-setup.md "Moved hook paths").
- Generated files: Claude's plugin loads its skills from `content/skills/`. An account that links skills into the kit removes those links: `tools/machine_setup.py` reports each one with the command that removes the link only (docs/machine-setup.md "Links"); a plugin already installed picks the folder up from its manifest at the next refresh. A kit checkout that wants plain `git commit` to render and stage the rendered files sets `core.hooksPath` to `githooks` (docs/content.md "Render on commit"); `tools/commit.py` does it with no setting.
- Plugin refresh (hosts/claude/shell/agents, content/skills, mcp/lock_server.py, mcp/hub_server.py; `tools/plugin_refresh.py`, docs/plugin-source.md) and any settings or .mcp.json change.
- Agents: plugin agents replace ~/.claude*/agents. First add the researcher exclusion removed from researcher.md to that repo's .claude/agent-profile.md; docs/ROSTER.md lists the two wording changes. sound-smith's opus-for-design tier comes from the agent roster.
- Repo switch-overs to the kit gate, lock and guard (each repository and the PM's).
- PM watcher: the PM runs the kit's `tools/pm_watch.py` and its repo drops `launcher/pm_watch.py` (docs/pm-watch.md "Switch-over for the PM repo").
- Launcher: `tools/launch.py` (docs/launcher.md) replaces the PM repo's `launcher/*.ps1`. Set the machine config's `launcher` key (docs/launcher.md "Settings"; `state_dir` is the folder holding the live sessions.json and coordinators.json, `coordinator_agent` at its default `coordinator`, which the launcher starts as `pm-kit:coordinator`, docs/launcher.md "The window"), re-register the after-reboot task with `launch.py install-autostart` (it registers `Claude coordinators after reboot` and unregisters the old `Claude conductors after reboot`), and move every caller (PM memory, hub commands, agent texts) to `launch.py`. The owner retires the old scripts. Add the sessions.json entry `kit` (repo `kit`, a launcher.repos name for this kit checkout; docs/launcher.sessions.example.json): the PM's write-jail keeps it out of kit worktrees, so kit work and landings need a kit coordinator, which `launch.py start --session kit --account current` starts on the account the PM is on. Run `install-autostart` from the main kit checkout, never a worktree: the task it registers runs the `tools/launch.py` it was run from (`kitlib/launcher/autostart.py`). Set `launcher.pm_resume_beside_desktop` true at switch-over (docs/launcher.md "Settings", "After a reboot"). After the switch, check live that the guard hook and the lock MCP server see the mark of a launcher-started deep-worker and pm session: an owner-only lock MCP tool called from it is refused with `rule=lock-mcp`, and `Stop-Process -Name <name>` with `rule=kill-by-name`.
- Guard write roots for the jailed sessions: the owner sets the machine config's `guard_mark_roots` under `pm` (each folder the PM writes outside its checkout and temp) before the launcher's `pm` mark goes live, and `roles.lander.edit_main_checkout: true` in the `.kit.json` of each repository whose lander fetches and fast-forwards its main checkout from a worktree (docs/guard.md "Write jail", "Go-live (after the joint review)" steps 3 and 7).
- Kind sizes: converting kinds.json from medium/heavy to whole numbers is a `gate.py --measure` proposal plus a reviewed commit. Once converted, `--cores` above the kind's size exits 2, a size past agent_cap stops every run (exit 10), and the kind no longer takes every slot inside an owner whole hold but its integer size, at most agent_cap.
- Hub repo aliases: set `aliases` on the hub store's repos/ docs for every short tag an old item still carries (docs/action-hub.md "repos/<name>").
- Retirements: heavy_run, test-runner wrappers. The global reserve_ui.pyw is replaced by the kit window `reserve_ui.pyw` at the kit root, which stays.
- Backlog store per repo: `backlog.py migrate`, then the switch-over branch (docs/backlog-store.md "Migration"). Until a repo's remote has a store, the landing close writes nothing and the ancestry check passes; the check still asks the remote (`ls-remote`) and refuses the landing when it cannot. A repo whose `.kit.json` still uses the older kit's spellings runs `backlog.py migrate` first, then moves the keys (docs/kit-json.md) on its switch-over branch.
- Switch-over command: `installer/machine-setup.ps1 -OldKit <old kit clone>` makes the per-account re-point below and the statusline, shortcut, temp ACL and comfy steps in one reviewed run, and reports each link an earlier setup left, with the command that removes it; it makes no link (dry run first, then `-Apply`; docs/machine-setup.md). On a machine with no older kit clone, `installer/machine-setup.ps1 -PmSettings <PM repo>\.claude\settings.local.json` (the default mode) appends the guard's entry, writes the PM SessionStart hook to that file alone, enables the plugin per account, stores the owner's email and runs the temp folder steps; it does not set `KIT_MACHINE_LOCK`, because the plugin's lock tool is used, and makes the schedule shortcut only with `-Shortcut` (docs/machine-setup.md "Default mode").
- Per-account settings re-point (owner runs these; PM drafts the commands):
  - Remove the old kit's PreToolUse hooks (no_root_search.py, main_checkout_redirect_guard.py) from settings.json in every account's config folder.
  - Add the guard's PreToolUse entry as `python <kit>/boot/hook.py claude --settings-entry` prints it (docs/guard.md "Go-live (after the joint review)").
  - Re-point each settings path that runs through an account's `tools` link (the status line's) to the kit's own file; machine setup plans it (docs/machine-setup.md "Settings"). Then remove the leftover links with the lines its dry run prints (docs/install.md "After the plugin install: remove the per-item junctions", Stage 3).
- Zero repo setup: machine setup is the only install step, and it writes nothing into any repository (but the `-PmSettings` file, when named). Owner action, once on each machine, from a terminal in the kit's main checkout (not a Claude session: the guard's `owner-setup` rule refuses `-Apply` to every session but the owner's own, since it stores the owner's email; docs/guard.md "Owner machine setup"): `powershell -NoProfile -File installer\machine-setup.ps1 -OwnerEmail <your commit email> [-PmSettings <PM repo>\.claude\settings.local.json]` (a machine set up from an older kit clone: `-OldKit <that clone>` instead), read the dry run, then the same with `-Apply`; restart the Claude sessions that were open. It adds the guard's entry, installs the Claude plugin per account (Codex: noted, not installed, hosts/codex/README.md), stores `owner_email` in the machine config (`-OwnerEmail`, or confirmed at the `-Apply` console question; `git config user.email` is only offered, never stored unconfirmed) and makes the agents' temp folder (docs/machine-setup.md "Default mode"). Until `owner_email` (or a repo's `"owner".email`) is set, every owner-only path refuses every edit. A repository then needs nothing committed: the first worktree or PM start writes `.git/info/exclude` and `core.hooksPath` locally (docs/install.md "What first use writes"). The schedule shortcut is now opt-in in the default mode (`-Shortcut`). Not yet run on a real machine or a live host.
- PM data branch (docs/pm-data-branch.md): migrated; still to confirm that it works as expected in use.

## Machine config keys

These keys in the machine config carry the install's names, paths and ledger ids (docs/kit-json.md "Machine config keys"). Set each before the hooks and tests go live:

1. `scrub_patterns`: the install's repository, owner and account names, one case-insensitive regex each. Until set, the shipped-text scrub checks only ruling ids, user-profile paths and the `machine_path_folders` paths.
2. `standing_delegation`: the ledger id of the standing delegation. Unset, the backlog refuses no id as the standing delegation.
3. `coordinator_start.repos_root`: the folder that holds the repositories. Unset, every coordinator check that needs the session's repo prints "could not check ...: repos_root not configured".
4. `coordinator_start.landing_lock`: {repo: landing-lock file name} for each repo that has a landing lock. Unset, the landing-lock line is dropped without a word.
5. Optional keys, off until set: `"eng_filing": true` under `coordinator_start` (the coordinator ENG standing line); the owner's flags as `eng_review` under `pm_start` (the PM-start line appears only once non-empty); `"pricing_pass": true` under `coordinator_start` (the coordinator's pricing line); `machine_lock.notice.dir` (lock-window notices, which the hub server lists; a wrapper killed outright writes no end notice); `machine_lock.daemons` `"stop"` or `"leave"` (docs/gate.md "Build daemons"); `agent_temp_sweep` (see the temp sweep below); `guard_mcp_prefix` and `guard_mcp_tools`.
6. `tools/ledger.py list` needs `ledger_path` (the PM's daily digest calls it).

## Fix before or at go-live

- worktree_remove.py and worktree_sweep_task.py (docs/worktree-remove.md): the owner registers the nightly sweep (Ported repo tools).
- Lock MCP tool names (the Claude plugin's MCP prefix, `kitlib.lockmcp.tool_prefix()`): no real PreToolUse payload captured. After the plugin install, read one and set `guard_mcp_prefix` and `guard_mcp_tools` if the names differ (docs/guard.md "Lock MCP").
- Pre-push non-fast-forward refusal (kitlib/guard/prepush.py; docs/guard.md "Pre-push"): refuses a non-ff push or delete of main wherever the hook is installed. A repo's own pre-push is deleted only once the kit's hook is on there.
- Owner-only paths (docs/guard.md "Owner-only commits"): the author email can be forged.
- Owner identity rule (docs/guard.md "Rules"): live with the guard. Open: an identity in the inherited environment.
- Lock release pre-check (tools/lock_release_check.py, docs/lock-release.md): the release step must run it, and the kit tests, before it moves the release checkout.
- Comfy compare tools (optional/comfy, docs/shipping.md): live once the optional tools ship (Skills, templates, optional tools).
- redfirst.py and daily_full.py (docs/redfirst.md, docs/daily-full.md): each repo runs them from the kit with `-C`. redfirst runs without `"redfirst".runners` in that repo's `.kit.json` (the defaults: docs/redfirst.md); written runners replace the defaults. daily_full needs `"daily_full".enabled` true plus a schedule the owner registers; the kit registers none. Repo-local copies retire only once the gaps in docs/daily-full.md "Not ported" are decided.
- Gate caller-checkout refusal (docs/gate.md "The caller's checkout"): existing stubs send no caller value, so in a repository that keeps stubs the refusal is off until `kit_stubs.py write` regenerates them (the kit form, `<kit>/tools/gate.py -C <checkout>`, needs none). A caller that passes `run --lock` gets exit 2, so callers change at switch-over.
- Agents' temp sweep (optional/agenttemp/agent_temp_sweep.py): live once `agent_temp_sweep` is set in the machine config and a scheduled task runs it with `--apply` (`tools/machine_setup.py --apply` sets both up, the task unless `--no-sweep-task`, after printing a dry run of the sweep; docs/machine-setup.md "Extras"); read that dry run's list. It sweeps each Claude Code session folder `claude/<project>/<session>` on its own; list any to keep in `agent_temp_sweep.keep`.
- Guard rules and defaults (docs/guard.md "Rules"; kitlib/config/policy.py HEAVY_DEFAULTS): live with guard.py's PreToolUse entry; `find -maxdepth 0` stays denied. Each repo lists its own heavy programs in `.kit.json` `heavy.programs` (docs/kit-json.md).
- Lock features that need the kit lock and its window live: bare `bash` in a job (runs Git Bash, refused when there is none), and Move to top (`machine_lock.py top ID`; not offered through the lock MCP server).
- Kit self tests: `tools/kit_tests.py --select` and `--jobs` (docs/test-tools.md "kit_tests") install nothing; kit briefs book `--kind engine-test --cores 2` for kit suites. Kit landings gate with `--select`. Owner switches for the daily full run: add `full_cmd` (running `--full`) to the kit-suite gate step, and enable `daily_full` in `.kit.json`; the owner also schedules daily_full.py, which the kit never schedules.
- A repo's `.claude/agents/<same name>` overriding a kit agent is resolved by Claude Code alone; no kit code resolves agent names, so the kit only models the rule (tests/agents/test_override.py). Confirm it once by hand at go-live.

## Joint-review questions

- PidHold in the gate: whether any gate step should ever run with the hold, given the measured cost above. Until decided, the gate refuses `--pid-hold`.
- `.claude/settings.json` for a new repository: the installer does not write one, and the kit ships no template. Decide whether `templates/common` carries one (hooks, permissions) and what it holds; until then the owner writes each repository's by hand.

## Ported repo tools

Each repo reaches them as `python <kit>/tools/<x>.py -C <checkout>`, with no copy or stub in the repo.

- Stubs are opt-in: an existing repo needs none, and its old stubs or tool copies can be deleted once its own scripts and docs name the kit form. A repo that keeps stubs regenerates them with `python <kit>/tools/kit_stubs.py write -C <repo> --replace`, commits, then `kit_stubs.py check -C <repo>` exits 0 (docs/install.md); the stubs carry the kit's absolute path, so a repo with a machine_paths check needs an allow entry for it.
- A new repo: `git init -b main`, an `origin` remote and a pushed main; nothing else (docs/install.md "Steps"). A repo that wants its kit settings committed runs the committed setup, `installer/new-repo.ps1 -Target <repo> -OwnerEmail <owner> -AgentEmail <agents>` or `tools/machine_setup.py --repo <repo> ...` (dry run), reviews, re-runs with `-Apply`, commits (docs/install.md "Committed setup (--repo)"). It refuses an existing `.kit.json`. Its `.kit.json` names the backlog store `"ref"`; a repo set up without it names its store before its first backlog write, which the default store refuses (docs/backlog.md "Stores").
- Owner identity: `.kit.json` "owner".email in each repo. commit.py `--owner-authored` and review_pack's owner-authored label need it; the PM kit's doc_budget.json identities are not read.
- machine_paths: the PM kit's built-in allow entries are gone; a repo adds `machine_paths.allow`/`fixtures` entries. A non-empty `machine_paths.piece_fixtures` is refused (docs/commit.md).
- pieces: a repo with a pieces table adds `"pieces": "tools/pieces.json"` to `.kit.json`; the file alone is not read.
- forbidden: create the store (`{}` when `.kit.json` names one), add tokens with `forbidden.py -C <repo> --add` (token on stdin), then a gate step (docs/repo-checks.md).
- trailers: add the `trailers.py` step with `merged_args` `--merged` and set `.kit.json` "owner".email in the same change (docs/repo-checks.md "trailers").
- pytests, tool_deps, testlist and kitlib.proc.shield: a repo adopts pytests through a gate step, never `python_tests` (docs/test-tools.md).
- PM record tools: the PM repo's `tools/pending.py`, its ledger tool's `standing` and `check`, and `hooks/pre-commit` retire once its callers point at the kit: set `pending_path` in the machine config (docs/pending.md), and for the hook set `"ledger": {"pre_commit_check": true}` in its `.kit.json` and its `core.hooksPath` to `<kit>/githooks` (docs/ledger.md "Pre-commit check").
- The nightly sweep: the owner registers it (`tools/worktree_sweep_task.py -C <repo main checkout> --register --owner-confirmed`); agents never type --owner-confirmed.
- The doc tools (docs/doc-tools.md): a repo's doc settings move from tools/doc_budget.json to `.kit.json` "doc_budget"/"doc_index", and doc_budget.json leaves the default owner paths. A repo needs no gate step for the index listing check; `.kit.json` "doc_index".enabled false skips it.

## Universal gate (tools/gate.py, kitlib/gate)

- Switch a repo only after its .kit.json lists every check as a gate step (Python tests included); the python_tests key alone selects nothing.
- push_block goes on in a repo only after it has landed once through `gate.py land`.
- Retire the repo's own gate and land wrappers when it switches (gate.py/land.py, a lander command).
- Hook modules load from the landing's base, the main it merges onto (docs/gate.md "Landing", step 6), one file each; sibling imports are unsupported.
- landing.log_dir inside a checkout must be git-ignored, or every landing is NOT LANDED.

## Skills, templates, optional tools, knowledge (owner-run; written, not performed)

Per Claude account.

1. Skills: the account's skills currently come from the old kit. Enable the plugin so skills load from
   `content/skills` (`claude plugin install` from the kit checkout, or add the kit as the account's plugin source),
   then remove the per-skill links as docs/install.md "After the plugin install: remove the per-item junctions" says.
   On a machine with no older kit clone, `machine-setup.ps1` enables it for every account and makes no per-skill links.
   Check with `ListSkills` that each skill name in docs/shipping.md appears once.
2. Statusline: in the account's `settings.json`, point `statusLine.command` at
   `optional/usage/statusline-usage.ps1` in the kit checkout, and
   any scheduled call of `write-usage-status.ps1` at the kit's copy. Machine setup plans the
   `statusLine` re-point, when the path names the older clone (`--old-kit`) or runs through a `tools` link (either mode)
   (docs/machine-setup.md "Settings"). Confirm the line renders.
3. Optional tools: point comfy, procwatch and usage entry points (shortcuts, scheduled tasks) at
   `optional/<name>` in the kit. Set env `PROCWATCH_PROTECT` for any extra processes procwatch must spare
   (the repo's live servers) and `KIT_MACHINE_LOCK` only if the lock tool is not the plugin's
   (`machine-setup.ps1` does not set it in its default mode; `-OldKit` does).
4. Knowledge: machine setup makes no account link to the kit's `knowledge/asset-generation`. A
   link an earlier setup made is reported as a leftover to remove; an account's own
   asset-generation folder is noted and left as it is (docs/machine-setup.md "Links").
5. Templates: new repos start from `templates/` (docs/shipping.md); no account setting changes.
6. Repo-local skills (a model training skill, worktree-start) stay in their repo's `.claude/skills`.

## Landing tool (tools/land.py)

One landing tool for every repo and lane; landings into a repo take turns, each one machine-lock entry with the resource `landing:<git common dir>` and priority `land` (docs/gate.md "The landing queue"). Per repo, after its gate is switched (Universal gate above):

1. `.kit.json`: set `repo.main_branch` and `repo.remote` when they are not `main` and `origin`. Declare each lane the repo's lander takes under `landing.lanes`, with the `merged_args` its merged-tree gate got for that lane. Set `landing.batch_max` to the most branches the repo lands together, and `landing.wait_s` when six hours is wrong (docs/kit-json.md).
2. A repo whose own lander keeps a lock file in the git common dir sets `landing.lock_file` to `git:<that name>` for the switch-over, so its old lander and land.py exclude each other on one file (kitlib.records.landing `lock_name`). The old lander's lock must be in the kit's record format, or land.py reads it as unreadable and waits on it; check that before both run. Drop the key once nothing else lands there.
3. Point every landing caller (lander agents, coordinators, auto-landers, landing scripts) at `python <kit>/tools/land.py BRANCH... --expect-tip SHA... [--lane L] [--wait S] -C <repo>` (hosts/claude/shell/agents/lander.md), or with `--submit` to return at once and wait on the printed `wait-result` command (docs/gate.md "Submitting a landing"). `--expect-main` is optional: every landing waits for its turn and merges onto `<remote>/<main>` as it is then (docs/gate.md "Landing", step 4). A script that merged several branches and gated once becomes one batch call. The repo's own land wrapper becomes a stub to the kit tool or is retired with the gate wrappers.
4. Check from the main checkout: `python <kit>/tools/land.py --queue` prints `landing lock: free` and `landing queue: empty`. Land one doc-only branch through land.py, then read its result record (`gate.py wait-result BRANCH --tip SHA --kind land -C <repo>`).
5. `gate.py land` stays as the one-branch form with `--wait` 0; move its callers to land.py.

## Coordinator rename

The agent, skill, hook module, profile file name and prose say coordinator. These live names still say conductor and keep working through fallbacks until renamed:

- Launcher: the old `start-conductor.ps1` passes `--agent conductor` and sets `PM_KIT_ROLE` to `conductor`; the kit's `launch.py` runs `launcher.coordinator_agent` (default `coordinator`, started as `pm-kit:coordinator`; it needs no `conductor` value) and always marks it `coordinator`. The session names still say conductor (`kitlib.coordinatorstart` accepts both roles; `kitlib.guard.rules.AGENT_MARKS` holds both marks; `optional/procwatch/procwatch.py` matches both agent names).
- Launcher state and task (W2a, done before the switch): rename `<state_dir>\conductors.json` to `coordinators.json` (the launcher refuses while only the old one is there; docs/launcher.md "State files"), run `install-autostart` from the main kit checkout (new task name; the old task is unregistered), and move each coordinator to a `coordinator` stem, once it replied "ready to hand off", with `launch.py handoff --session <key> --stem <repo>-coordinator --recycle` (same account) or `... --stem <repo>-coordinator --account N`, then the `stop` line it prints for the old `<repo>-conductor-...` name (docs/launcher.md "Hand-off"). Until W2b, `start --session` and `after-reboot` refuse or skip an entry while a session under its old conductor stem runs.
- `~/.claude/settings.json` (done): the SessionStart command called the old-path stub `hooks/conductor_session_start.py`; it now names `hosts/claude/shell/hooks/coordinator_session_start.py`, and the stub and `tests/oldpaths`' conductor cases are deleted.
- The machine config: key `conductor_start` becomes `coordinator_start` (read as a fallback; the new key wins); then drop the `conductor_start` field in `kitlib/config/user.py`, `LEGACY_KEY` and `LEGACY_ROLE` in `kitlib/coordinatorstart`, and the `conductor` entry in `AGENT_MARKS`.
- Each repo: `.claude/conductor-profile.md` becomes `.claude/coordinator-profile.md` (also in each repo's doc-budget `capped` list), `docs/ops/conductor-runbook.md` becomes `coordinator-runbook.md`, and the owner-only CLAUDE.md and `.claude` files lose the word.
- Installed copies: `~/.claude/agents/conductor.md` (replaced by the plugin's coordinator.md), `~/.claude/tools/usage/conductor_profile.py` and its test, the `conductor-dispatch` skill, `~/.claude/conductor-profile.TEMPLATE.md`.
