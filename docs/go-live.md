# Go-live list

Held for the joint review with the owner. Nothing here happens before it.
The code is on kit main except the lines marked unbuilt; each line is a switch that makes it live, a decision, or something unbuilt.

## Switches

- Hooks: changes to claude/hooks/ or kitlib/pmstart, kitlib/coordinatorstart (all accounts load them from this checkout).
- Claude folder move: `hooks/pm_session_start.py`, `hooks/coordinator_session_start.py` and `tools/guard.py` now live under `claude/hooks/`, and `hub/action-hub.html` under `claude/hub/`. Every account's SessionStart (and any PreToolUse guard) command and the machine config's `hub_source` must name the new paths, or the hooks stop running.
  - Old paths work meanwhile: `hooks/pm_session_start.py` and `hooks/coordinator_session_start.py` are forwarding stubs, and `hub/action-hub.html` is a byte-identical copy (tests/oldpaths). Cleanup, after that switch: delete `hooks/*.py`, `hub/action-hub.html`, `tests/oldpaths/` and its selection.json entries.
- Plugin install (claude/agents, claude/skills, mcp/lock_server.py) and any settings or .mcp.json change. It also brings the agent and skill text already on main: the coordinator restart wording, creative agents options-first, the refusal-template text and the researcher exclusion.
- Agents: plugin agents replace ~/.claude*/agents. First add the researcher exclusion removed from researcher.md to that repo's .claude/agent-profile.md; docs/ROSTER.md lists the two wording changes. sound-smith's opus-for-design tier comes from the agent roster.
- Repo switch-overs to the kit gate, lock and guard (each repository and the PM's).
- PM watcher: PM repo launcher/pm_watch.py becomes the stub in docs/pm-watch.md "Stub plan for the PM repo".
- Launcher: `tools/launch.py` (docs/launcher.md) replaces the PM repo's `launcher/*.ps1`. Set the machine config's `launcher` key (docs/launcher.md "Settings"; `state_dir` is the folder holding the live sessions.json and conductors.json, `coordinator_agent` `conductor` until the coordinator rename), re-register the after-reboot task with `launch.py install-autostart` (same task name, so it replaces the old one), and move every caller (PM memory, hub commands, agent texts) to `launch.py`. The owner retires the old scripts. Run `install-autostart` from the main kit checkout, never a worktree: the task it registers runs the `tools/launch.py` it was run from (`kitlib/launcher/autostart.py`). For the joint review: (1) the deep-worker, consultant, pm and plain marks are not in the guard's agent marks (`kitlib.guard.rules.AGENT_MARKS`: coordinator, conductor), so the kill-by-name and lock-state rules treat those sessions as the owner's unless `guard_agent_marks` lists them; whether to list them is a decision for the review. (2) `kitlib.launcher.reboot.desktop_app_running` skips the PM resume when any claude.exe runs from the Store folder or without a Remote Control name, so a stray unnamed CLI session also skips it (docs/launcher.md "After a reboot"); kept as built.
- Kind sizes: converting kinds.json from medium/heavy to whole numbers is a `gate.py --measure` proposal plus a reviewed commit. Once converted, `--cores` above the kind's size exits 2, a size past agent_cap stops every run (exit 10), and the kind no longer takes every slot inside an owner whole hold but its integer size, at most agent_cap.
- Action Hub To PM: republish claude/hub/action-hub.html to the live artifact (the `to_pm` collection needs no new capability; restate `parked` rules if the db rules are declared again), then teach the PM session start to list `to_pm` docs with status `sent`.
- Hub repo aliases: before republishing the hub, set `aliases` on the hub's repos/ docs for every short tag the old map covered; the page no longer carries that map in canonRepo.
- Retirements: heavy_run, test-runner wrappers. The global reserve_ui.pyw is replaced by the kit window `reserve_ui.pyw` at the kit root, which stays.
- PM data branch (docs/pm-data-branch.md): live when `tools/pm_data.py init` (or `--adopt` for an existing branch) runs on the PM's data repository, jointly with the owner, with a private remote that is not the public one ("Making the branch"). Needs tasks 2-8 on kit main ("Tasks"); the guard rule `git-push-pmdata` and the pre-push leak stage guard it once `kit-pm-data.json` exists ("Pre-push guard"); the data worktree's `.kit.json` must set the data branch, private remote and a non-`ref` backlog store ("Layout").
- Backlog store per repo: `backlog.py migrate`, then the switch-over branch (docs/backlog-store.md "Migration"). Until a repo's remote has a store, the landing close writes nothing and the ancestry check passes; the check still asks the remote (`ls-remote`) and refuses the landing when it cannot. `backlog.py` refuses two repos' current `.kit.json`: they still use the old PM-kit spellings, refused on purpose (tests/config/test_load.py `test_the_old_hook_and_owner_spellings_refuse`). The new spellings are in docs/kit-json.md and docs/doc-tools.md. The PM kit cannot load them, so each repo runs `backlog.py migrate` first, then makes the key move on its switch-over branch.
- Switch-over command: `installer/machine-setup.ps1 -OldKit <old kit clone>` makes the per-account re-point below and the links, statusline, shortcut, temp ACL and comfy steps in one reviewed run (dry run first, then `-Apply`; docs/machine-setup.md).
- Per-account settings re-point (owner runs these; PM drafts the commands):
  - Remove the old kit's PreToolUse hooks (no_root_search.py, main_checkout_redirect_guard.py) from settings.json in every account's config folder.
  - Add guard.py's PreToolUse entry as `python <kit>/claude/hooks/guard.py --settings-entry` prints it (docs/guard.md "Go-live (after the joint review)").
  - Re-point each account's ~/.claude/tools link (the live lock tool), where it has one, from the old kit to the kit checkout.

## Machine config keys from the generic pass

These keys in `~/.claude-pm-kit/config.json` carry the install's names, paths and ledger ids (docs/kit-json.md "`~/.claude-pm-kit/config.json` keys"). Set each before the hooks and tests go live:

1. `scrub_patterns`: the install's repository, owner and account names, one case-insensitive regex each. Until set, the shipped-text scrub (tests/shipped.py, tests/agents/test_agents.py) checks only ruling ids and user and machine paths, and passes. tests/scrub/test_generic.py also reads it, with the names it derives from `pm_start` and `coordinator_start`, and prints "scrub_patterns is not set" while it is unset.
2. `standing_delegation`: the ledger id of the standing delegation. Unset, the backlog refuses no id as the standing delegation.
3. `coordinator_start.repos_root`: the folder that holds the repositories. Unset, every coordinator check that needs the session's repo prints "could not check ...: repos_root not configured".
4. `coordinator_start.ledger`: the PM rulings ledger file (or set the machine `ledger_path`). Neither set: "could not check rulings: ledger not configured".
5. `coordinator_start.landing_lock`: {repo: landing-lock file name} for each repo that has a landing lock. Unset, the landing-lock line is dropped without a word.
6. Optional keys, off until set: `"eng_filing": true` under `coordinator_start` (the coordinator ENG standing line); the owner's flags as `eng_review` under `pm_start` (the PM-start line appears only once non-empty); `"pricing_pass": true` under `coordinator_start` (needs the agents install); `machine_lock.notice.dir` (lock-window hub notices, which the PM posts with hub_notices.py; a wrapper killed outright writes no end notice); `machine_lock.daemons` `"stop"` or `"leave"` (the Gradle, Kotlin, MSBuild and compiler-server daemons are otherwise stopped only after a gate run that ends "done"); `agent_temp_sweep` (see the temp sweep below); `guard_mcp_prefix` and `guard_mcp_tools`.
7. `tools/ledger.py list` needs `ledger_path` (the PM's daily digest calls it).

## Fix before or at go-live

- worktree_remove.py and worktree_sweep_task.py (docs/worktree-remove.md): the owner registers the nightly sweep (Ported repo tools).
- PM docs/machine-lock.md: add the line on docker exec runs that hold no gpu slot (docs/lock-api.md "Runs").
- Lock MCP owner writes: the mark is built (kitlib/sessionmark.py, docs/guard.md "Session mark") and guard.py and the lock MCP server read it. The kit launcher (`tools/launch.py`, docs/launcher.md "The window") marks every session it starts: coordinator, deep-worker, consultant, pm, plain. Live with the Launcher switch above. Until then those sessions count as the owner's own, and owner writes through MCP come back forbidden because api.who sees claude.exe as the caller.
- Lock MCP tool names (mcp__plugin_claude-pm-kit_machine-lock__*): no real PreToolUse payload captured. After the plugin install, read one and set `guard_mcp_prefix` and `guard_mcp_tools` if the names differ (docs/guard.md "Lock MCP").
- Pre-push non-fast-forward refusal (kitlib/guard/prepush.py; docs/guard.md "Pre-push"): refuses a non-ff push or delete of main wherever the hook is installed. A repo's own pre-push is deleted only once the kit's hook is on there.
- Owner-only paths (docs/guard.md "Owner-only commits"): the author email can be forged.
- Owner identity rule (docs/guard.md "Rules"): live with the guard. Open: an identity in the inherited environment.
- Lock release pre-check (tools/lock_release_check.py, docs/lock-release.md): the release step must run it, and the kit tests, before it moves the release checkout.
- Comfy compare tools (optional/comfy, docs/shipping.md): live once the optional tools ship (Skills, templates, optional tools).
- redfirst.py and daily_full.py (docs/redfirst.md, docs/daily-full.md): each repo reaches them through stubs. redfirst needs that repo's `"redfirst".runners` in `.kit.json`. daily_full needs `"daily_full".enabled` true plus a schedule the owner registers; the kit registers none. Repo-local copies retire only once the gaps in docs/daily-full.md "Not ported" are decided.
- Gate caller-checkout refusal (docs/gate.md "The caller's checkout"): existing stubs send no caller value, so the refusal is off in a repository until `kit_stubs.py write` regenerates its stubs. The `run --lock` flag is not added to the kit's run, which always locks; a caller that passes it gets exit 2, so callers change at switch-over.
- `run --lock` advice: the live `~/.claude/tools/machine_lock/machine_lock.py` requires `--lock` on `run`. When the kit tool is installed at that path, drop `--lock` from CLAUDE.md's heavy-runs line, the docs/guard.md `heavy` row, `LOCK_RUN_FLAGS` in kitlib/guard/rules.py, and the guard test rows (tests/guard/test_rules.py, tests/guard/shell_rows.py) in the same change.
- For the owner's review: the caller value (`KIT_STUB_CALLER`) carries the stub's checkout path and the caller's directory. tools/gate.py removes it; any other kit tool run through a moved stub passes both paths on to its child processes.
- Agents' temp sweep (optional/agenttemp/agent_temp_sweep.py): tested on scratch folders only, never run on the real one. Live once `agent_temp_sweep` is set in the machine config and a scheduled task runs it with `--apply`; run it once without `--apply` first and read the list.
- Guard rules and defaults (docs/guard.md "Rules"; kitlib/config/policy.py HEAVY_DEFAULTS): live with guard.py's PreToolUse entry; `find -maxdepth 0` stays denied. Each repo lists its own heavy programs in `.kit.json` `heavy.programs` (docs/kit-json.md). No heavy-run scanner or daily audit.
- Lock features that need the kit lock and its window live: bare `bash` in a job (runs Git Bash, refused when there is none), and Move to top (`machine_lock.py top ID`; not offered through the lock MCP server). The live-client lock kind is not built (the owner's decision); a live client books `engine-run`.
- Kit self tests: `tools/kit_tests.py --select` and `--jobs` (docs/test-tools.md "kit_tests") install nothing; kit briefs book `--kind engine-test --cores 4` for kit suites. Kit landings gate with `--select`. Owner switches for the daily full run: add `full_cmd` (running `--full`) to the kit-suite gate step, and enable `daily_full` in `.kit.json`; the owner also schedules daily_full.py, which the kit never schedules.
- A repo's `.claude/agents/<same name>` overriding a kit agent is resolved by Claude Code alone; no kit code resolves agent names, so the kit only models the rule (tests/agents/test_override.py). Confirm it once by hand at go-live.

## Joint-review questions (gap audit 10-07)

Settled:

- `find <path> -maxdepth 0` stays refused.
- Kit backlog: records generic to the kit move to the kit's own store (re-scoped first if needed); install-specific records stay with the install.
- Heavy runs: no scanner and no audit; the guard's heavy list is the mechanism, and each repo lists its own programs.
- PidHold stays out of the gate. It is opt-in for agent test runs only (`--pid-hold` on tools/kit_tests.py and tools/pytests.py, off by default), and the gate refuses the flag (kitlib/gate/steps.py `unsupported`). Its whole-suite cost is measured in docs/test-tools.md "What the hold costs" (2026-10-10): the 3 s warm-up per run, with no steady cost above run-to-run noise, solo or with 2 runs at once.
- Pin-check / offline hatch: a per-repo gate hook; the kit provides only the hook point.
- Stack runners (check.py, js_tests.py, dotnet_each.py) are not kit tools; each repo keeps them as its own gate hooks.
- No slash commands; skills cover them.
- The lander may fetch and fast-forward merge in the main checkout.
- Each repo's port audit was folded into its `.kit.json` migration (checked complete).
- Refusal exit code: the kit's worktree-removal tools exit 1 on a refusal and repos adapt.
- Owner identity: push checks cover main only; there is no other-branch check.

Open:

- PidHold in the gate: whether any gate step should ever run with the hold, given the measured cost above. Until decided, the gate refuses `--pid-hold`.
- `.claude/settings.json` for a new repository: the installer does not write one, and the kit ships no template. Decide whether `templates/common` carries one (hooks, permissions) and what it holds; until then the owner writes each repository's by hand.

## Ported repo tools

On kit main, none live. Each repo reaches them through stubs.

- Stubs per existing repo: `python <kit>/tools/kit_stubs.py write -C <repo> --replace`, commit, then `kit_stubs.py check -C <repo>` exits 0 (docs/install.md). The stubs carry the kit's absolute path, so a repo with a machine_paths check needs an allow entry for it.
- A new repo: `git init -b main`, then `installer/new-repo.ps1 -Target <repo> -OwnerEmail <owner> -AgentEmail <agents>` (dry run), review, re-run with `-Apply`, commit (docs/install.md). It refuses an existing `.kit.json`.
- Owner identity: `.kit.json` "owner".email in each repo. commit.py `--owner-authored` and review_pack's owner-authored label need it; the PM kit's doc_budget.json identities are not read.
- machine_paths: the PM kit's built-in allow entries are gone; a repo adds `machine_paths.allow`/`fixtures` entries. A non-empty `machine_paths.piece_fixtures` is refused (docs/commit.md).
- pieces: a repo with a pieces table adds `"pieces": "tools/pieces.json"` to `.kit.json`; the file alone is not read.
- forbidden: create the store (`{}` when `.kit.json` names one), add tokens with `forbidden.py -C <repo> --add` (token on stdin), then a gate step (docs/repo-checks.md).
- trailers: add the `trailers.py` step with `merged_args` `--merged` and set `.kit.json` "owner".email in the same change (docs/repo-checks.md "trailers").
- ruling_cites: the machine config `ledger_path` must be set or the step only warns; then a gate step.
- pytests, tool_deps, testlist and kitlib.proc.shield: a repo adopts pytests through a gate step, never `python_tests` (docs/test-tools.md).
- PM repo: its kit/tools copies of the ported tools become stubs or are dropped.
- PM record tools: the PM repo's `tools/pending.py`, `tools/ledger_refs.py`, its ledger tool's `standing` and `check`, and `hooks/pre-commit` retire once its callers point at the kit: set `pending_path` and `ledger_refs` in the machine config (docs/pending.md, docs/ledger.md "ledger_refs"), and for the hook set `"ledger": {"pre_commit_check": true}` in its `.kit.json` and its `core.hooksPath` to `<kit>/githooks` (docs/ledger.md "Pre-commit check"). A kit older than this port refuses a `.kit.json` holding a `ledger` section (unknown sections are refused), so add it only once every kit checkout that reads that repo has the port.
- The nightly sweep: the owner registers it himself after joint review (`tools/worktree_sweep_task.py -C <repo main checkout> --register --owner-confirmed`); agents never type --owner-confirmed.
- The doc tools (tools/doc_budget.py, doc_index.py, codemap.py; docs/doc-tools.md) are held until the joint review. A repo's doc settings move from tools/doc_budget.json to `.kit.json` "doc_budget"/"doc_index", and doc_budget.json leaves the default owner paths. A repo needs no gate step for the index listing check; `.kit.json` "doc_index".enabled false skips it.

## Universal gate (tools/gate.py, kitlib/gate)

- Switch a repo only after its .kit.json lists every check as a gate step (Python tests included); the python_tests key alone selects nothing.
- push_block goes on in a repo only after it has landed once through `gate.py land`.
- Retire the repo's own gate and land wrappers when it switches (gate.py/land.py, a lander command).
- Hook modules load from --expect-main, one file each; sibling imports are unsupported.
- landing.log_dir inside a checkout must be git-ignored, or every landing is NOT LANDED.
- Batch landing exists (`land.py A B`, docs/gate.md "Batch"); one repo's gate reuse is ported (docs/gate.md "Gate reuse"): a repo opts steps in with `reuse`.
- guard.py and pre-push read records with runner.verify_record: a record without config_rev does not count, so a commit gated before config_rev existed is gated again before push_block goes on.

## Skills, templates, optional tools, knowledge (owner-run; written, not performed)

Per Claude account, once reviewed. Nothing here has been run.

1. Skills: the account's skills currently come from the old kit. Remove that link (junction or copy) from
   `~/.claude/skills`, then enable the plugin so skills load from `<kit>/skills`
   (`claude plugin install` from the kit checkout, or add the kit as the account's plugin source).
   Check with `ListSkills` that the ten names in docs/shipping.md appear once each.
2. Statusline: in the account's `settings.json`, point `statusLine.command` at
   `optional/usage/statusline-usage.ps1` in the kit checkout (it was the old kit's copy), and
   any scheduled call of `write-usage-status.ps1` at the kit's copy. Confirm the line renders.
3. Optional tools: point comfy, procwatch and usage entry points (shortcuts, scheduled tasks) at
   `optional/<name>` in the kit. Set env `PROCWATCH_PROTECT` for any extra processes procwatch must spare
   (the repo's live servers) and `KIT_MACHINE_LOCK` only if the lock tool is not the plugin's.
4. Knowledge: replace `~/.claude/knowledge/asset-generation` with a link to
   `knowledge/asset-generation` in the kit checkout (keep the old folder until the link is verified;
   its LOG.md and samples are not in the kit).
5. Templates: new repos start from `templates/` (docs/shipping.md); no account setting changes.
6. Repo-local skills (a model training skill, worktree-start) stay in their repo's `.claude/skills`.

## Landing tool (tools/land.py)

One landing tool for every repo and lane; landings into a repo queue first come, first served (docs/gate.md "The landing queue"). Per repo, after its gate is switched (Universal gate above):

1. `.kit.json`: set `repo.main_branch` and `repo.remote` when they are not `main` and `origin`. Declare each lane the repo's lander takes under `landing.lanes`, with the `merged_args` its merged-tree gate got for that lane. Set `landing.batch_max` to the most branches the repo lands together, and `landing.wait_s` when an hour is wrong (docs/kit-json.md).
2. A repo whose own lander keeps a lock file in the git common dir sets `landing.lock_file` to `git:<that name>` for the switch-over, so its old lander and land.py exclude each other on one file (kitlib.records.landing `lock_name`). The old lander's lock must be in the kit's record format, or land.py reads it as unreadable and waits on it; check that before both run. Drop the key once nothing else lands there.
3. Point every landing caller (lander agents, coordinators, auto-landers, landing scripts) at `python <kit>/tools/land.py BRANCH... --expect-main SHA --expect-tip SHA... [--lane L] --wait S` (claude/agents/lander.md). An auto-lander passes `--wait` so it waits for its turn instead of racing and, when it had to wait, lands at its turn even though main moved forward meanwhile (docs/gate.md "Landing", step 4). A script that merged several branches and gated once becomes one batch call. The repo's own land wrapper becomes a stub to the kit tool or is retired with the gate wrappers.
4. Check from the main checkout: `python <kit>/tools/land.py --queue` prints `landing lock: free` and `landing queue: empty`. Land one doc-only branch through land.py, then read its result record (`gate.py wait-result BRANCH --tip SHA --kind land`).
5. `gate.py land` stays as the one-branch form with `--wait` 0; move its callers to land.py.

## Coordinator rename

The agent, skill, hook module, profile file name and prose say coordinator. These live names still say conductor and keep working through fallbacks until renamed:

- Launcher: the old `start-conductor.ps1` passes `--agent conductor` and sets `PM_KIT_ROLE` to `conductor`; the kit's `launch.py` runs `launcher.coordinator_agent` (set it to `conductor` until the rename) and always marks it `coordinator`. The session names still say conductor (`kitlib.coordinatorstart` accepts both roles; `kitlib.guard.rules.AGENT_MARKS` holds both marks; `optional/procwatch/procwatch.py` matches both agent names).
- `~/.claude/settings.json`: the SessionStart command still calls the old-path stub `hooks/conductor_session_start.py`; point it at `claude/hooks/coordinator_session_start.py`, then delete the stub and `tests/oldpaths`' conductor cases.
- `~/.claude-pm-kit/config.json`: key `conductor_start` becomes `coordinator_start` (read as a fallback; the new key wins); then drop the `conductor_start` field in `kitlib/config/user.py`, `LEGACY_KEY` and `LEGACY_ROLE` in `kitlib/coordinatorstart`, and the `conductor` entry in `AGENT_MARKS`.
- Each repo: `.claude/conductor-profile.md` becomes `.claude/coordinator-profile.md` (also in each repo's doc-budget `capped` list), `docs/ops/conductor-runbook.md` becomes `coordinator-runbook.md`, and the owner-only CLAUDE.md and `.claude` files lose the word.
- Installed copies: `~/.claude/agents/conductor.md` (replaced by the plugin's coordinator.md), `~/.claude/tools/usage/conductor_profile.py` and its test, the `conductor-dispatch` skill, `~/.claude/conductor-profile.TEMPLATE.md`.
