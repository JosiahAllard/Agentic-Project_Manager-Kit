# Go-live list

The kit is live and the go-live holds are released: three repositories, this kit among them, land through `gate.py land` on the owner's machine. What remains is each switch still to make, each decision still open and each fix still to land. Machine install is docs/install.md; the machine setup modes are docs/machine-setup.md.

## Switches

- Action Hub site: the claude.ai artifact hub is retired and its page deleted. The owner goes live in this order. (1) Trial first, with no `hub` key: `tools/hub.py serve --store <scratch folder>`; a trial never writes the `hub` key. (2) Write the `hub` key in the machine config: `store` an absolute path to a new, empty folder (or leave it unset for `hub/` in the PM data worktree; docs/pm-data-branch.md), plus any `port` change. (3) Start `tools/hub.py serve` and check `hub.py status`. (4) Drop `pm_start.hub_url`, `hub_urls` and `hub_source` from the machine config (the checklist no longer reads them). Optional: reach the hub from a phone (docs/hub-remote-access.md) and start it at logon (`hub.py setup --autostart`, docs/action-hub.md "Serving it").
- Agents: plugin agents replace `~/.claude*/agents`. First add any exclusion a repo's profile carried for the retired researcher to that repo's `.claude/agent-profile.md`; the `analyst` (read) and `docs-writer` (docs) types replace it, and the creative work runs on a worker type with a `specialty-*` skill (senior-worker at minimum for 3d-assets and tech-art; worker, or senior-worker for design, for sound) (docs/specialty-skills.md); docs/ROSTER.md lists the wording changes.
- Specialty cutover (docs/specialty-skills.md "Build split", branch B): seven agent types are gone (asset-smith, tech-artist, sound-smith, researcher, reviewer, records, spec-writer). Refresh the plugin after it lands (`tools/plugin_refresh.py`, docs/plugin-source.md), and land it only when nothing that dispatches an old type name is running (a stale dispatch fails in the host). Owner to-dos (docs/specialty-skills.md "Owner to-do"): 1, the `kitlib/backlog/roles.py` module docstring in this kit; 2 and 3, for each downstream repo, the `.kit.json` `doc_index.roles` keys and the profile lines naming a retired type.
- Repo switch-overs to the kit gate, lock and guard (each repository and the PM's).
- PM watcher: the PM runs the kit's `tools/pm_watch.py` and its repo drops `launcher/pm_watch.py` (docs/pm-watch.md "Switch-over for the PM repo").
- Launcher: `tools/launch.py` (docs/launcher.md) replaces the PM repo's `launcher/*.ps1`. Set the machine config's `launcher` key (docs/launcher.md "Settings"; `state_dir` is the folder holding the live sessions.json and coordinators.json, `coordinator_agent` at its default `coordinator`, which the launcher starts as `pm-kit:coordinator`), re-register the after-reboot task with `launch.py install-autostart` (it registers `Claude coordinators after reboot`), and move every caller (PM memory, hub commands, agent texts) to `launch.py`. The owner retires the old scripts. Add the sessions.json entry `kit` (repo `kit`, a launcher.repos name for this kit checkout; docs/launcher.sessions.example.json): the PM's write-jail keeps it out of kit worktrees, so kit work and landings need a kit coordinator, which `launch.py start --session kit --account current` starts on the account the PM is on. Run `install-autostart` from the main kit checkout, never a worktree: the task it registers runs the `tools/launch.py` it was run from (`kitlib/launcher/autostart.py`). `kitlib.launcher.reboot.desktop_app_running` skips the PM resume when any claude.exe runs from the Store folder or without a Remote Control name; the optional key `launcher.pm_resume_beside_desktop` (default false) turns that skip off, and the owner sets it true at switch-over (docs/launcher.md "Settings", "After a reboot"). After the switch, check live that the guard hook sees the mark of a launcher-started deep-worker and pm session (the `[kit guard: ...]` tag says only `role=main` there, `kitlib/guard/rules.py` `tag_role`): a retired owner-only lock MCP tool name called from it is refused with `rule=lock-mcp` and a reason that says `This session is deep-worker.` or `This session is pm.` (`rules.lock_mcp`), and `Stop-Process -Name <name>` from it is refused with `rule=kill-by-name`, which the owner's unmarked session is not.
- Guard write roots for the jailed sessions: the owner sets the machine config's `guard_mark_roots` under `pm` (each folder the PM writes outside its checkout and temp) before the launcher's `pm` mark goes live, and `roles.lander.edit_main_checkout: true` in the `.kit.json` of each repository whose lander fetches and fast-forwards its main checkout from a worktree (docs/guard.md "Write jail", "Go-live (after the joint review)" steps 3 and 7).
- Kind sizes: converting kinds.json from medium/heavy to whole numbers is a `gate.py --measure` proposal plus a reviewed commit. Once converted, `--cores` above the kind's size exits 2, a size past agent_cap stops every run (exit 10), and the kind no longer takes every slot inside an owner whole hold but its integer size, at most agent_cap.
- Hub repo aliases: set `aliases` on the hub store's repos/ docs for every short tag an old item still carries (docs/action-hub.md "repos/<name>").
- Retirements: heavy_run, test-runner wrappers. The global reserve_ui.pyw is replaced by the kit window `reserve_ui.pyw` at the kit root, which stays.
- Backlog store per repo: the switch-over branch (docs/backlog-store.md "Migration"). Until a repo's remote has a store, the landing close writes nothing and the ancestry check passes; the check still asks the remote (`ls-remote`) and refuses the landing when it cannot. `backlog.py` refuses two repos' current `.kit.json`: they still use the older kit's spellings, refused on purpose (tests/config/test_load.py `test_the_old_hook_and_owner_spellings_refuse`). The new spellings are in docs/kit-json.md and docs/doc-tools.md; each repo makes the key move on its switch-over branch.
- PM data branch (docs/pm-data-branch.md): the PM's data repository moved onto the data branch with `tools/pm_data.py init --adopt` on 2026-10-11, with a private remote that is not the public one ("Making the branch"). Still to confirm: that it works as expected in use. The guard rule `git-push-pmdata` and the pre-push leak stage guard it once `kit-pm-data.json` exists ("Pre-push guard"); the data worktree's `.kit.json` must set the data branch, private remote and a non-`ref` backlog store ("Layout").

## Coordinator rename

The kit no longer reads the old name anywhere. What is left outside the kit:

- Each repo: `.claude/conductor-profile.md` becomes `.claude/coordinator-profile.md` (also in each repo's doc-budget `capped` list), `docs/ops/conductor-runbook.md` becomes `coordinator-runbook.md`, and the owner-only CLAUDE.md and `.claude` files lose the word.
- Installed copies: `~/.claude/agents/conductor.md` (replaced by the plugin's coordinator.md), `~/.claude/tools/usage/conductor_profile.py` and its test, the `conductor-dispatch` skill, `~/.claude/conductor-profile.TEMPLATE.md`.

## Machine config keys

Keys in the machine config are in docs/kit-json.md "Machine config keys" (`scrub_patterns`, `coordinator_start.repos_root`, `guard_mcp_prefix`, the ledger keys). Optional keys that file does not list, off until set:

- `"eng_filing": true` under `coordinator_start` (the coordinator ENG standing line).
- `eng_review` under `pm_start` (the owner's flags; the PM-start line appears only once non-empty).
- `"pricing_pass": true` under `coordinator_start` (`PRICING_LINE` in `kitlib/coordinatorstart/__init__.py`: at start it tells the coordinator to price the other unpriced open records 2-5 and leave follow-up records unpriced until promoted into a work set).
- `"gate_cap": {"<repo>": n}` under `coordinator_start` (docs/gate.md "The session gate cap": at most n gate runs of one repo in flight per agent session; unset, 0 or null: no cap).
- `machine_lock.daemons` `"stop"` or `"leave"` (otherwise a Gradle or Kotlin daemon is stopped only after a gate run that ends "done", and only when it serves nothing outside that run's job; MSBuild nodes and the compiler server are always left: docs/gate.md "Build daemons").
- `guard_mcp_tools` (with `guard_mcp_prefix`).

## Fix before or at go-live

- worktree_remove.py and worktree_sweep_task.py (docs/worktree-remove.md): machine setup registers each launcher repository's `<repo>-worktree-sweep` task by default (docs/machine-setup.md "Extras"); `worktree_sweep_task.py --register --owner-confirmed` is the by-hand form, and agents never type `--owner-confirmed`.
- PM docs/machine-lock.md: add the line on docker exec runs that hold no gpu slot (docs/lock-api.md "Runs").
- Lock MCP tool names (the Claude plugin's MCP prefix, `kitlib.lockmcp.tool_prefix()`): no real PreToolUse payload captured. After the plugin install, read one and set `guard_mcp_prefix` and `guard_mcp_tools` if the names differ (docs/guard.md "Lock MCP").
- Pre-push non-fast-forward refusal (kitlib/guard/prepush.py; docs/guard.md "Pre-push"): refuses a non-ff push or delete of main wherever the hook is installed. A repo's own pre-push is deleted only once the kit's hook is on there.
- Owner-only paths (docs/guard.md "Owner-only commits"): the author email can be forged.
- Owner identity rule (docs/guard.md "Rules"): live with the guard. Open: an identity in the inherited environment.
- Lock release pre-check (tools/lock_release_check.py, docs/lock-release.md): the release step must run it, and the kit tests, before it moves the release checkout.
- redfirst.py and daily_full.py (docs/redfirst.md, docs/daily-full.md): each repo runs them from the kit with `-C`. redfirst runs without `"redfirst".runners` in that repo's `.kit.json` (the defaults: docs/redfirst.md); written runners replace the defaults. daily_full needs `"daily_full".enabled` true plus a schedule the owner registers; the kit registers none. Repo-local copies retire only once the gaps in docs/daily-full.md "Not ported" are decided.
- The `run --lock` flag is not added to the kit's run, which always locks; a caller that passes it gets exit 2, so callers change at switch-over.
- Agents' temp sweep (optional/agenttemp/agent_temp_sweep.py): live once `agent_temp_sweep` is set in the machine config and the task runs it with `--apply` (`tools/machine_setup.py --apply` sets both up; docs/machine-setup.md "Extras"); read the dry run's list and name any session folder to keep in `agent_temp_sweep.keep`.
- Guard rules and defaults (docs/guard.md "Rules"; kitlib/config/policy.py HEAVY_DEFAULTS): `find -maxdepth 0` stays denied. Each repo lists its own heavy programs in `.kit.json` `heavy.programs` (docs/kit-json.md). No heavy-run scanner or daily audit.
- Lock features that need the kit lock and its window live: bare `bash` in a job (runs Git Bash, refused when there is none), and Move to top (`machine_lock.py top ID`; not offered through the lock MCP server). The live-client lock kind is not built (the owner's decision); a live client books `engine-run`.
- Kit self tests: `tools/kit_tests.py --select` and `--jobs` (docs/test-tools.md "kit_tests") install nothing; kit briefs book `--kind engine-test --cores 2` for kit suites. Kit landings gate with `--select`. Owner switches for the daily full run: add `full_cmd` (running `--full`) to the kit-suite gate step, and enable `daily_full` in `.kit.json`; the owner also schedules daily_full.py, which the kit never schedules.
- A repo's `.claude/agents/<same name>` overriding a kit agent is resolved by Claude Code alone; no kit code resolves agent names, so the kit only models the rule (tests/agents/test_override.py). Confirm it once by hand.

## Joint-review questions

Settled:

- `find <path> -maxdepth 0` stays refused.
- Kit backlog: records generic to the kit move to the kit's own store (re-scoped first if needed); install-specific records stay with the install.
- Heavy runs: no scanner and no audit; the guard's heavy list is the mechanism, and each repo lists its own programs.
- PidHold stays out of the gate: opt-in for agent test runs only (`--pid-hold` on tools/kit_tests.py and tools/pytests.py), and the gate refuses the flag (kitlib/gate/steps.py `unsupported`); its cost is measured in docs/test-tools.md "What the hold costs".
- Pin-check / offline hatch: a per-repo gate hook; the kit provides only the hook point.
- Stack runners: dotnet_each.py and js_tests.py ship in the stack templates; check.py stays a repo's own gate hook pending an owner answer.
- No slash commands; skills cover them.
- The lander may fetch and fast-forward merge in the main checkout.
- Refusal exit code: the kit's worktree-removal tools exit 1 on a refusal and repos adapt.
- Owner identity: push checks cover main only; there is no other-branch check.

Open:

- PidHold in the gate: whether any gate step should ever run with the hold, given the measured cost. Until decided, the gate refuses `--pid-hold`.
- `.claude/settings.json` for a new repository: the installer does not write one, and the kit ships no template. Decide whether `templates/common` carries one (hooks, permissions) and what it holds; until then the owner writes each repository's by hand.

## Ported repo tools

Each repo reaches the kit's tools as `python <kit>/tools/<x>.py -C <checkout>`, with no copy or stub in the repo (decision 21 in docs/multi-host-plan.md section 7). A new repo needs nothing; a repo that wants its kit settings committed runs the committed setup (docs/install.md "Committed setup (--repo)"). Per repo:

- An existing repo's old stubs or tool copies can be deleted once its own scripts and docs name the kit form; the kit writes and checks none.
- Owner identity: `.kit.json` "owner".email in each repo. commit.py `--owner-authored` and review_pack's owner-authored label need it; the PM kit's doc_budget.json identities are not read.
- machine_paths: the PM kit's built-in allow entries are gone; a repo adds `machine_paths.allow`/`fixtures` entries. A non-empty `machine_paths.piece_fixtures` is refused (docs/commit.md).
- pieces: a repo with a pieces table adds `"pieces": "tools/pieces.json"` to `.kit.json`; the file alone is not read.
- forbidden: create the store (`{}` when `.kit.json` names one), add tokens with `forbidden.py -C <repo> --add` (token on stdin), then a gate step (docs/repo-checks.md).
- trailers: add the `trailers.py` step with `merged_args` `--merged` and set `.kit.json` "owner".email in the same change (docs/repo-checks.md "trailers").
- pytests, tool_deps, testlist and kitlib.proc.shield: a repo adopts pytests through a gate step; `python_tests` is no gate key (docs/test-tools.md).
- PM repo: its kit/tools copies of the ported tools are dropped.
- PM record tools: the PM repo's `tools/pending.py`, its ledger tool's `standing` and `check`, and `hooks/pre-commit` retire once its callers point at the kit: set `pending_path` in the machine config (docs/pending.md), and for the hook set `"ledger": {"pre_commit_check": true}` in its `.kit.json` and its `core.hooksPath` to `<kit>/githooks` (docs/ledger.md "Pre-commit check"). A kit older than this port refuses a `.kit.json` holding a `ledger` section (unknown sections are refused), so add it only once every kit checkout that reads that repo has the port.
- The doc tools (tools/doc_budget.py, doc_index.py, codemap.py; docs/doc-tools.md): a repo's doc settings move from tools/doc_budget.json to `.kit.json` "doc_budget"/"doc_index", and doc_budget.json leaves the default owner paths. A repo needs no gate step for the index listing check; `.kit.json` "doc_index".enabled false skips it.

## Universal gate (tools/gate.py, kitlib/gate)

- Switch a repo only after its .kit.json lists every check as a gate step (Python tests included).
- push_block goes on in a repo only after it has landed once through the kit's landing (`land.py`; `gate.py land` is its one-branch form, docs/gate.md "Landing").
- Retire the repo's own gate and land wrappers when it switches (gate.py/land.py, a lander command).
- Hook modules load from the landing's base, the main it merges onto (docs/gate.md "Landing", step 6), one file each; sibling imports are unsupported.
- landing.log_dir inside a checkout must be git-ignored, or every landing is NOT LANDED.
- Batch landing exists (`land.py A B`, docs/gate.md "Batch"); a repo opts steps into gate reuse with `reuse` (docs/gate.md "Gate reuse").
- guard.py and pre-push read records with runner.verify_record: a record without config_rev does not count, so a commit gated before config_rev existed is gated again before push_block goes on.

## Skills, templates, optional tools, knowledge (owner-run)

Per Claude account, once reviewed:

1. Skills: enable the plugin so skills load from `content/skills` (docs/install.md "Plugin install"; machine setup does it for every account). Remove each leftover per-skill link (docs/install.md "Existing installs"). Check with `ListSkills` that the skill names in docs/shipping.md appear once each.
2. Statusline: point `statusLine.command` at `optional/usage/statusline-usage.ps1` in the kit checkout, and any scheduled call of `write-usage-status.ps1` at the kit's copy. Machine setup plans the re-point when the path names the older clone (`--old-kit`) or runs through a `tools` link (docs/machine-setup.md "Settings"). Confirm the line renders.
3. Optional tools: point comfy, procwatch and usage entry points (shortcuts, scheduled tasks) at `optional/<name>` in the kit. Set env `PROCWATCH_PROTECT` for any extra processes procwatch must spare (the repo's live servers) and `KIT_MACHINE_LOCK` only if the lock tool is not the plugin's (`-OldKit` sets it; the default mode does not).
4. Knowledge: an account's own asset-generation folder is noted and left (docs/machine-setup.md "Links"). Its LOG.md and samples are not in the kit, so move what is worth keeping before removing it.
5. Repo-local skills (a model training skill, worktree-start) stay in their repo's `.claude/skills`.

## Landing tool (tools/land.py)

One landing tool for every repo; landings into a repo take turns, each one machine-lock entry with the resource `landing:<git common dir>` and priority `land` (docs/gate.md "The landing queue"). Per repo, after its gate is switched (Universal gate above):

1. `.kit.json`: set `repo.main_branch` and `repo.remote` when they are not `main` and `origin`. Set `landing.batch_max` to the most branches the repo lands together, and `landing.wait_s` when six hours is wrong (docs/kit-json.md).
2. The landing lock is always `kit-landing.lock` in the git common dir (kitlib.records.landing `LOCK_NAME`); no key names another (`landing.lock_file` is retired and ignored, docs/kit-json.md). A repo whose own lander keeps a lock file of its own retires that lander before land.py lands there, with no landing running, queued or stuck: the two locks do not exclude each other.
3. Point every landing caller (lander agents, coordinators, auto-landers, landing scripts) at `python <kit>/tools/land.py BRANCH... --expect-tip SHA... [--wait S] -C <repo>` (hosts/claude/shell/agents/lander.md), or with `--submit` to return at once and wait on the printed `wait-result` command (docs/gate.md "Submitting a landing"). `--expect-main` is optional (docs/gate.md "Landing", step 4). A script that merged several branches and gated once becomes one batch call. The repo's own land wrapper is retired with the gate wrappers.
4. Check from the main checkout: `python <kit>/tools/land.py --queue` prints `landing lock: free` and `landing queue: empty`. Land one doc-only branch through land.py, then read its result record (`gate.py wait-result BRANCH --tip SHA --kind land -C <repo>`).
5. `gate.py land` stays as the one-branch form with `--wait` 0; move its callers to land.py.
