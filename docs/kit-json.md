# `.kit.json`

A repository's kit configuration, read and checked whole by `kitlib.config.load` (`load_at` reads it at a revision: a landing reads the base branch's). The schema is `kitlib/config/schema.py`; tests/config/test_user_docs.py keeps the lists below equal to it.

- A section is an object or, except `repo`, `owner` and `hook`, a string naming a repo-relative JSON file that holds it.
- A section is read only from `.kit.json` or the file it names: no `tools/*.json` is read because it exists.
- An unknown section or key refuses, and so does a key written twice in one object. One error names every problem. A key starting `_`, or `comment`, is a note, allowed in every object.
- A key whose default is unset may be `null`. Unset means the tool that reads it decides.
- No environment variable overrides the file or the repository root.
- With no `gate` section at all the gate detects the stack and runs its usual test and lint steps, labelled `source: auto` (docs/gate.md "Auto-detected gate"); a `gate` section, even an empty one, replaces that whole.
- A `gate.steps` entry runs either `cmd` or `check`, a gate check the repo hook module provides in `GATE_CHECKS`; repo-specific gate settings go in `hook.options`. The step keys are `STEP` in `kitlib/config/schema.py`.
- `gate.kind` names the kind a gate run books; the lock books that kind's size (kitlib/lock/kinds.json). A size is a whole number of cpu slots 1..the layout's agent_cap (`medium` and `heavy` remain as aliases), set only by `gate.py --measure` and a reviewed commit. No `.kit.json` key sets a gate's core count.
- A `landing.lanes` entry is keyed by the lane name and has four keys (`LANE` in `kitlib/config/schema.py`): `branch_prefix` (list of string; a branch on the lane starts with one), `paths` (list of glob; every path the branch changes matches one), `why` (string; the ruling that made the lane) and `merged_args` (list of string; added to every step command of the lane's merged-tree gate, after the step's own `merged_args`, with the same `{root}`-style substitutions). `tools/land.py` reads `main_branch` and `remote` from `repo`, the lanes, `wait_s` and `batch_max` from `landing`, and the gate steps from `gate` (docs/gate.md, Landing).
- No old spelling is read: the hook module is the top-level `hook` (not `gate.hook`), and owner settings are `owner` (not `doc_budget` keys).
- `backlog.code_main` matters where the store's checkout is on a branch that shares no history with the code, as the kit's pmdata store is (its `repo.main_branch` is `pmdata`): there `backlog.py add --found-on <branch>` (`plan_add` in `kitlib/backlog/plan.py`) finds no merge base and stops with an ERROR naming the key, until the PM sets it to the code's main branch. In the PM data worktree itself an unset key takes the main checkout's `repo.main_branch`, and the code main is read on the main checkout's `repo.remote` (`code_config` in `kitlib/backlog/pull_branch.py`, used by `open_store` in `kitlib/backlog/opener.py`; docs/backlog.md "The fold-in guard"). `--found-on none` needs no base.
- Retired keys are still accepted and ignored, so an older install loads: `backlog` may still carry `retired_docs`, `rename_script` and `ledger_repo` (`RETIRED` in `kitlib/config/schema.py`); nothing reads them. A whole `ruling_cites` section is accepted the same way and dropped unchecked (`RETIRED_SECTIONS` there); any other unknown section still refuses.
- A repo lists its own heavy programs in `heavy.programs`; the kit defaults are general tools only (godot, blender, dotnet, gradle). For example `{"program": "run_tests.bat", "kind": "engine-test", "why": "runs the engine test suite"}`, `{"program": "graph_build*", "kind": "worldgraph", "why": "rebuilds the world graph"}` and `{"program": "local_model*", "kind": "local-ai", "unless_args": ["--help"], "why": "loads a model onto the gpu"}`.

## With no file

`.kit.json` is optional: a repository with none is all defaults, and every key a file writes is an override. A file that is there but does not parse, or holds an unknown section or key or a wrong type, still refuses every call that reads it (the guard, the gate, land, commit, pre-push), as before.

Three defaults are read rather than fixed (`kitlib.config.load`, from `kitlib.git.defaults` and the machine config). Each applies whenever the file does not set the key, with a file or without one:

- `repo.main_branch`: the branch `refs/remotes/<remote>/HEAD` points at (`git clone` writes it; `git remote set-head origin --auto` writes it in a clone that has none). With no such ref: `main` when `refs/heads/main` or `<remote>/main` exists, else `master` when only that exists, else `main`. Read from the git files (the `.git` folder, or the `gitdir:` file of a linked worktree and its `commondir`; loose refs and `packed-refs`), never by starting git; a repository whose refs are in a reftable is asked once (`git symbolic-ref`). A `<remote>/HEAD` naming a branch that starts with `-` or `/` or holds `..` is not read. Read once per load, so a guard hook pays one file read or two for it.
- `repo.name`: the last part of the remote's URL in the repository's config, `.git` dropped; with no remote, the main checkout's folder name. The URL is the first one in the common config, else, while that config turns `extensions.worktreeConfig` on, the first one in the work tree's own `config.worktree` (`kitlib/git/defaults.py` `remote_url`). It names the gate log folder (`<kit temp>/<name>-gate`), the worktree sweep's log and scheduled task, and the lock line's `--repo`; `landed.py` reads the gate logs under each checkout's folder name too, where a gate wrote them before the name was read from the remote.
- `owner.email`: the machine config's `owner_email` (below). With neither, or with a machine config that cannot be read, no commit can be proven to be the owner's, so every change to an owner-only path is refused; it is never allowed. `tools/machine_setup.py` stores it (`--owner-email`, or the address the owner confirms at its `--apply` console question, which offers `git config user.email`; docs/machine-setup.md "Owner email").

Protections on with no file:

- No delete and no forced push of the main branch (`--force`, `--force-with-lease`, `--force-if-includes`, `--mirror`, a `+` refspec), from any session: the guard refuses it (rule `git-push-main`), and the pre-push hook refuses a delete or a push that is not a fast-forward of the remote's main whatever `repo.push_block` says. The guard and the pre-push hook name the main branch the defaults above give, and, while the file sets no `repo.main_branch`, guard `main` as well (`Config.guarded_mains`): `<remote>/HEAD` is local, and any session can repoint it (`git remote set-head`), so reading it never takes the protection off `main`. A refspec the guard cannot read literally (`git push origin $B`, `"+$B"`, `HEAD:$B`) is refused, since it may force or delete main; so is a computed remote with `--force`, `--delete` or `--no-verify`.
- The owner-only paths (`owner.paths` default) and the owner identity rule: with the owner email from the machine config, an agent's change to an owner-only path is refused unless the owner approved it, and a session giving a commit the owner's email is refused.
- The rules whose defaults are already the strict side: the kit-json rule (an agent session never writes a `.kit.json`), the write jail (no data roots: an agent writes its own checkout and temp only), the main-checkout rule (no subagent edits a main checkout), the lock rules and the kit's own heavy-program list.

Off with no file, each switched on by its key: `repo.push_block` (a push to main needs a gate-OK record; it needs a gate), `guard.role_policy` (holding roles to their mode; the kit's own repo turns it on, and the Codex broker holds its role to it whatever the file says), `roles.lander.edit_main_checkout` (a lander fast-forwarding the main checkout), `ledger.pre_commit_check`, `daily_full.enabled` and `landing.in_flight`. The doc index listing check runs only in a repository with an index file (`docs/INDEX.md`, or the one `doc_index.index` names), with or without a `.kit.json`; `doc_index.enabled` false turns it off (docs/doc-tools.md "doc_index").

## `.kit.json` keys

- `repo.name` (string, may be empty, default ""): repository name for reports; unset or empty: the last part of the remote's URL, else the main checkout's folder name
- `repo.main_branch` (string, default "main"): the branch landings target; unset: the branch <remote>/HEAD names, else main, else master when only that exists (docs/kit-json.md "With no file")
- `repo.remote` (string, default "origin"): the remote main is pushed to
- `repo.worktrees_dir` (repo path, default ".claude/worktrees"): where agent worktrees live
- `repo.worktrees_by_host` (map host -> repo path, default {}): per-host worktree folders, keyed by hosts.json host name (docs/new-worktree.md "Hosts")
- `repo.tools_dir` (repo path, default "tools"): where repo tools, the opt-in kit tool stubs and the hook module live
- `repo.push_block` (bool, default false): pre-push refuses a push to main without a gate-OK record; a delete or a forced push of main is refused whatever it says
- `owner.email` (email): author email of the owner's own commits; unset: the machine config's owner_email; neither: no owner commit is provable, so every owner-only change is refused
- `owner.agent_emails` (list of email, default []): author emails known to be agents', never the owner's
- `owner.paths` (list of string, default ["AGENTS.md", "CLAUDE.md", ".claude/", ".codex/", "docs/agent-profile.md", ".github/workflows/", ".kit.json", "tools/gate.json", "kitlib/gate/strict_defaults.py", "kitlib/backlog/roles.py"]): owner-only paths; an entry ending / locks a directory; replaces the default
- `owner.paths_except` (list of string, default [".claude/worktrees/", ".codex/worktrees/"]): what under a locked directory is not locked
- `owner.trailer` (a trailer key, default "Owner-Approved"): the trailer key approving an owner-only change
- `hook.module` (a module name): the one repo hook module, <tools_dir>/<module>.py
- `hook.options` (map key -> any JSON value, default {}): passed to the hook unread
- `heavy.programs` (list of {program, kind, when_args, unless_args, why}, default []): repo heavy programs, added to the kit defaults
- `roles` (map role -> {edit_main_checkout, mode, why}): agent type (or main, subagent) -> permissions, over the kit defaults
- `roles.<role>.edit_main_checkout` (bool): may edit files in a main checkout; unset: the base role (main: true, any other: false)
- `roles.<role>.mode` (controller|write|read|test|lander): the role policy this role is held to, over its role file's `mode` (docs/guard.md "Role policy"); unset: the role file's, else none
- `roles.<role>.why` (string): the ruling that grants it
- `backlog.store` (ref|branch|main or repo path, default "ref"): the record store: ref (refs/backlog/main), branch (the backlog branch), main (docs/backlog) or a folder (docs/backlog.md); the default ref is read-only, so a repository that writes records names its store
- `backlog.phases` (list of string, default []): phase names in order
- `backlog.id_grammar` (upper|mixed, default "upper"): item id spelling
- `backlog.code_main` (string): the branch code lands on, which `add --found-on` measures a branch's diff from; default repo.main_branch
- `board.state_file` (repo path): the board state file, under the git dir (default <git common dir>/kit/board-state.md)
- `checkout_guard.rule_pointer` (string): where a refusal points for the rule
- `commit.merge_fold` (bool, default false): commit.py folds a merge commit
- `commit.coauthor_families` (list of string, default []): accepted Co-Authored-By families beyond Claude
- `daily_full.enabled` (bool, default false): daily_full.py runs at all (opt-in; it is never scheduled by the kit)
- `daily_full.ref` (string): the ref it tests; default <remote>/<main branch>
- `daily_full.worktree` (repo path): its detached worktree; default <worktrees_dir>/daily-full
- `daily_full.kind` (a kind name, default "daily-full"): the kinds-table kind the run books
- `daily_full.max_wait` (int >= 0, default 7200): seconds it waits for its lock slot before it skips
- `daily_full.stale_hours` (int >= 1, default 36): hours after which --summary calls the last result stale
- `daily_full.bisect` ({max_probes, prefix, session}): after a red run, bisect the first-parent commits since the last green run for the commit that turned the red steps red (docs/daily-full.md "Bisect"); unset: no bisect
- `doc_budget.default` (int >= 0): byte cap for a capped doc with no budget
- `doc_budget.budgets` (map path -> int >= 0 or null, default {}): doc path or glob -> byte cap; null: uncapped
- `doc_budget.uncapped_dirs` (list of repo path, default []): folders never capped
- `doc_budget.capped` (list of repo path, default []): docs that must stay under budget
- `doc_budget.retired_paths` (list of repo path, default []): paths that must not come back
- `doc_budget.shard_indexes` (map folder -> repo path, default {}): shard folder -> its index doc
- `doc_budget.state_docs` (list of repo path, default []): docs checked for staleness
- `doc_budget.stale_days` (int >= 1): days before a state doc is stale
- `doc_index.enabled` (bool, default true): false: doc_index does not require every .md on an index entry line, even with an index file; with no index file the check is skipped anyway (the index file is the opt-in)
- `doc_index.index` (repo path, default "docs/INDEX.md"): the doc index both doc tools read
- `doc_index.find_match` (keywords|text, default "keywords"): how --find matches
- `doc_index.roles` (map role -> list of string): role -> lookup topics
- `doc_index.roles_doc` (repo path): doc holding the role map instead
- `doc_index.roles_marker` (string): line before the role bullets in roles_doc
- `doc_index.role_lines` (int >= 1, default 80): lines printed per role section
- `forbidden`: a table the kit passes through unchecked (a file path or an object)
- `gate.steps` (list of step object, default []): the repo gate steps
- `gate.setup` (list of string): argv run before the steps
- `gate.python_tests` (discover|pytests, default "discover"): how the Python tests run
- `gate.python_tests_floor` (int >= 0): fewest tests a full run may report
- `gate.trailers` (bool): check commit trailers
- `gate.worktree_guard` (bool): refuse to gate in a main checkout
- `gate.warn_untracked` (bool): warn about untracked files
- `gate.fail` (regex or list of regex): log patterns that fail every step
- `gate.warn_lines` (regex or list of regex): log patterns that warn
- `gate.zero_tests` (regex or list of regex): log patterns meaning no test ran
- `gate.errors` ({match, excuse, expect}): error-line policy
- `gate.kind` (a kind name, default "engine-test"): the kinds-table kind a gate run books (gate.py and gate.py land)
- `gate.smoke_steps` (list of a step name): steps a default (not --full) branch gate runs whatever changed; a landing takes such a gate's record for a tip once these steps passed in it (docs/gate.md "Landing"); unset: a landing needs a --full record
- `gate.docs_only` ({globs, exclude, steps}): a change whose every path is docs: the branch gate runs only its steps, and a landing takes that record (docs/gate.md "Docs-only changes"); unset: no change is docs-only
- `guard.data_roots` (list of {path, mode, why}, default []): folders besides their checkout and temp that agent sessions may write, each with its mode; read from the main checkout only (docs/guard.md "Write jail")
- `guard.role_policy` (bool, default false): hold roles to their role policy mode (docs/guard.md "Role policy"); off: no role is held, whatever its mode; read from the main checkout only
- `landing.lock_file` (string): legacy landing lock: git:<name>, a file in the git common dir; any other value refuses
- `landing.log_dir` (repo path): where gate logs live
- `landing.lanes` (map lane -> {branch_prefix, paths, why, merged_args}, default {}): lanes besides full that gate.py land --lane takes
- `landing.wait_s` (int from 0 to 86400, default 21600): seconds tools/land.py waits for its turn (its machine-lock booking, then the landing lock) unless --wait says otherwise
- `landing.batch_max` (int from 1 to 20, default 3): most branches one tools/land.py landing merges and gates together
- `landing.reuse_max_age_h` (int from 0 to 168, default 24): hours a pass stored in the gate cache serves a branch gate rerun or a landing; 0: no gate reads or stores a step result
- `landing.reuse_never` (list of string, default []): globs of paths whose change makes a gate (branch or landing) read and store no step result in the gate cache
- `landing.in_flight` ({wait_s, command}): how tools/new_worktree.py and tools/sync_base.py see a landing in flight before they fetch, and wait for it (unset: off; {}: the kit landing lock; wait_s unset: 100, inferred not measured; docs/new-worktree.md "Waiting for a landing in flight")
- `ledger.pre_commit_check` (bool, default false): githooks/pre-commit runs tools/ledger.py check --base HEAD before each commit that changes the ledger file or its standing digest (opt-in; docs/ledger.md "Pre-commit check")
- `machine_paths.allow` (list of {glob, line, hit, why, temporary}, default []): machine-path hits allowed
- `machine_paths.fixtures` (list of {glob, hits, line, why}, default []): test fixtures that name machine paths
- `machine_paths.piece_fixtures` (list of {piece, path, hits, why}, default []): fixtures named by piece; machine_paths.py refuses a non-empty list until it resolves pieces
- `machine_paths.known_binary` (map path -> string, default {}): binary file -> why it may hold paths
- `machine_paths.scan` (list of string, default ["*"]): globs scanned
- `machine_paths.fallback_vars` (list of string, default []): environment variables a machine path may fall back to
- `new_worktree`: no keys
- `pieces`: a table the kit passes through unchecked (a file path or an object)
- `preflight.engines` (list of string, default []): process-name prefixes listed as running engines
- `preflight.report_hooks` (list of "module:function", default []): module:function report hooks
- `preflight.traps` (string): where the traps are written down
- `preflight.udp_ports` (list of 2 int from 0 to 65535): [low, high] ports checked free
- `redfirst.runners` (list of {glob, kind, pythonpath, dir, project, csproj, args, lock}, default [{"glob": "tests/*test_*.py", "kind": "python"}]): which runner takes a test file, first match wins; none written: this default, then <tools_dir>/*test_*.py as python with the tools folder as pythonpath, unless tools_dir is . or that glob is listed (docs/redfirst.md); a runner's lock is the kinds-table kind the run books around its command (unset: the kind of the heavy-list entry the command matches)
- `redfirst.hold_wait` (int >= 0, default 60): seconds a held throwaway tree is re-checked before it is left
- `review_pack.coauthor` (bool, default false): list the branch's commits with no Co-Authored-By trailer, judged by the trailers rule; a failing one blocks
- `stubs.tools` (list of string): kit tools stubbed into the repo; stubs are opt-in (none by default), and setting this opts in
- `testlist.junit` (list of string, default ["**/build/test-results/**/*.xml"]): JUnit result globs
- `testlist.trx` (list of string, default ["**/*.trx"]): .NET result globs
- `tool_tests.mentions` (path|tokens, default "path"): how a tool mention is found
- `tool_tests.selector_files` (list of string, default ["tool_deps.py", "pytests.py"]): changes that select every test
- `tool_tests.extra` (map file -> list of string, default {}): file -> test modules it selects
- `tool_tests.pinned` (list of [path, [modules]], default []): path -> modules pins
- `tool_tests.patterns` (list of {regex, modules}, default []): path regex -> modules
- `tool_tests.whole_tree` (map test class -> string, default {}): classes reading the whole tree -> why
- `tool_tests.untested` (list of string, default []): modules with no test, each listed
- `tool_tests.dynamic_imports` (map module -> [modules, why], default {}): computed imports allowed
- `tool_tests.guard_tests` (list of string, default ["test_tool_deps"]): tests guarding the table
- `tool_tests.unmapped_tool` (guard|all, default "guard"): what an unmapped tool change selects
- `tool_tests.always_run` (list of string, default []): tests every run includes
- `tool_tests.sources` (list of string, default []): token mode: source globs
- `tool_tests.skip_dirs` (list of string, default ["__pycache__"]): folders never scanned
- `tool_tests.ignore` (list of string, default []): changes that select nothing
- `tool_tests.shards` (map module -> int >= 1, default {}): pytests: module -> shard count
- `tool_tests.serial` (list of string, default []): pytests: modules or classes run alone
- `tool_tests.split_classes` (bool, default false): pytests: one unit per class
- `tool_tests.jobs_max` (int >= 1, default 6): pytests: most parallel jobs
- `tool_tests.jobs_divisor` (int >= 1, default 4): pytests: cores per job
- `tool_tests.jobs_env` (string): pytests: env var overriding the job count
- `tool_tests.unit_timeout` (int >= 1, default 3600): pytests: seconds per unit
- `worktree_remove.exempt` (map folder -> string, default {}): worktree folder -> the ruling exempting it
- `worktree_remove.disposable_untracked` (list of string, default []): untracked file globs removal may discard
- `worktree_remove.build_output` (list of string or {piece, path}, default []): build output path globs, or {piece, path} folders resolved through the pieces table
- `worktree_remove.keepers` (list of string, default []): file globs that refuse a removal, added to the kit list
- `worktree_remove.archive_next_free` (bool, default false): archive under the next free name
- `worktree_remove.branch_template` (a branch name holding {slug} once or list of a branch name holding {slug} once): branch name for a slug, or a list of them (a worktree on any one is removable)

## Machine config keys

The machine's own settings, read by `kitlib.config.user`: `config.json` in the folder `kitlib.config.user.DIR_NAME` names under the home folder, or, while that folder has none, in the retired folder ("Moving the machine config folder"); `python -c "from kitlib.config import user; print(user.config_file())"`, run in the kit, prints the path of the file read. Messages name the file read (`user.display()`), help text `user.DISPLAY`, and this page and the others write `<config-dir>` for that folder. No `.kit.json` key and no ledger environment variable overrides them. `~` follows USERPROFILE on Windows and HOME elsewhere (`os.path.expanduser`), so an environment that changes that variable reads another machine config. One key does not follow it: `machine_lock` is read from the config under the profile folder Windows itself reports (`kitlib.lock.site.load`, through `kitlib.lock.store.machine_home`), where the machine's lock store lives, so every session that shares the store reads one `machine_lock`, whatever its USERPROFILE. Every other key is read under the environment's home.

A retired key is still accepted and ignored, so an older machine config loads: `ledger_refs` (`RETIRED` in `kitlib/config/user.py`); nothing reads it. Any other unknown key still refuses.

Each kit copy on the machine reads this one file with its own key list, so a key "older kit copies reject" refuses the whole file in every copy that does not know it. `python tools/hub.py setup --check` lists each copy (the plugin export folders, the pins and each account folder's install, `kitlib.config.copies`) with the keys of the file plus `hub` that it would refuse, and writes nothing. It reads each copy's `SCHEMA` and `RETIRED` from the text of its `kitlib/config/user.py` and checks top-level keys only.

- `owner_email` (email): the author email of the owner's own commits, for every repository whose .kit.json sets no "owner".email; unset there too: no commit is provable as the owner's, so every owner-only change is refused (docs/kit-json.md "With no file"); older kit copies reject the key
- `ledger_dir` (absolute path): the rulings ledger folder: one file `<ledger_dir>/<repo>.md` per `ledger_repos` name, which it needs; set here only (docs/ledger.md "Files"); older kit copies reject the key; set it only after every folder runs a kit that knows it
- `ledger_repos` (list of a lower-case repo name): the repos `ledger.py add --repo` writes; `pm` is the machine-wide one; a repository's name is its folder name lower-cased with `_` as `-` (`My_Tools-Repo` is `my-tools-repo`); with `ledger_dir`, each name once, and never `all`, `standing` or the frozen file's name; without it, the one-file ledger's repo columns besides `all`, and unset: any lower-case name (docs/ledger.md)
- `ledger_path` (absolute path): the one-file rulings ledger (R-nnnn ids); set here only; once `ledger_dir` is set it is frozen: read, never written; removed one release after the split
- `pending_path` (absolute path): the owner's pending-steps file tools/pending.py edits (docs/pending.md)
- `standing_delegation` (a ruling id R-nnnn): the ledger entry that delegates in general; the backlog refuses it as the ruling behind any one change. Unset: no id is refused for that
- `daily_red_ruling` (a ruling id R-nnnn): the ruling the daily full run's bisect files its red record under: priority 1, priority_by pm (docs/daily-full.md "Bisect"); unset: priority 2, priority_by agent, and the run's result.json says so; older kit copies reject the key; set it only after every folder runs a kit that knows it
- `scrub_patterns` (list of regex): case-insensitive patterns for the install's own names (repositories, owner, accounts) that the shipped-text tests and tests/scrub/test_generic.py also refuse
- `guard_mcp_prefix` (string): the lock MCP tool-name prefix the guard denies owner-only tools under; unset: the kit default (docs/guard.md "Lock MCP")
- `guard_mcp_tools` (list of string): more full tool names the guard treats as the lock MCP's owner-only tools, as a PreToolUse payload names them (docs/guard.md "Lock MCP")
- `guard_agent_marks` (list of string): the session marks the guard treats as agent sessions for the kill-by-name, lock-state, kit-json and write-jail rules; it replaces the default list, so it names every mark it holds; unset: coordinator, conductor (removed after the switch), deep-worker, consultant and pm (docs/guard.md "Session mark")
- `guard_mark_roots` (map mark -> list of {path, mode, why}): more write-jail roots for the main sessions of one launcher mark, as {mark: [{path, mode, why}]}; a root that is, holds or lies inside either machine config folder, the current or the retired one, is ignored unless it is read-only (docs/guard.md "Write jail")
- `pm_data` (object, checked by kitlib.pmdata): the worktree of the PM data branch: {path, branch, remote} (docs/pm-data-branch.md "Layout")
- `hub` (object, checked by kitlib.hubsite.settings): the Action Hub site settings: port, tunnel_port, store, commit_every_s, push (unset: on only when store is unset and pm_data is set), backlog_refresh_s, access: {team, aud, emails}, relay: {source}; unset: every default, and the site still runs; older kit copies reject the key; set it only after every folder runs a kit that knows it
- `pm_start` (object, checked by kitlib.pmstart): the PM session-start checklist settings (docs/pm-start.md "Config")
- `coordinator_start` (object, checked by kitlib.coordinatorstart): the coordinator session-start settings
- `conductor_start` (object, checked by kitlib.coordinatorstart): the old name of coordinator_start, read when coordinator_start is absent; removed after the switch
  Its install values (`kitlib.coordinatorstart`; nothing in the code supplies them):
  - `repos_root` (path): the folder whose top-level folders are the repositories; the session's cwd picks the repo under it. Unset: every check that needs the coordinator's repo prints "could not check ...: repos_root not configured".
  - `landing_lock` ({repo: file name}): the landing lock's file in that repo's git common dir, shown as the landing-lock line. A repo not named here, or an unset key, gets no landing-lock line, and nothing says it was dropped.
- `pm_watch` (object, checked by kitlib.pmwatch): the PM watcher settings
- `launcher` (object, checked by kitlib.launcher): the session launcher settings: state folder, repos, account folders (docs/launcher.md "Settings")
- `backlog_lint` (object, checked by kitlib.backloglint): the backlog lint repositories
- `machine_lock` (object, checked by kitlib.lock.site): the machine lock settings: build daemons at release, lock-window notices
  Read from the config under the Windows profile folder, not under the environment's home (the paragraph above); under the kit's tests the read is refused (docs/test-tools.md "Home fence").
- `temp_dir` (absolute path): the folder the kit's own temp files go in (test run folders, gate logs); unset: `kit-temp` at the root of the checkout's drive, or the shared temp folder on its drive (docs/test-tools.md "Temp folders"); older kit copies reject the key
- `machine_path_folders` (list of a folder name): the drive-rooted folder names the machine-paths check refuses besides a user profile (docs/commit.md "machine_paths.py"); unset: none, so only drive-rooted user-profile paths are refused and tools/machine_paths.py prints a NOTE saying so; older kit copies reject the key; set it only after every folder runs a kit that knows it
- `agent_temp_sweep` (object, checked by optional/agenttemp/agent_temp_sweep.py): the agents' temp folder to sweep (`dir`), the age past which an unused entry goes (`max_age_days`, default 3) and the entries it never removes (`keep`, relative paths such as `claude/<project>/<session>`); `tools/machine_setup.py` adds it when absent (docs/machine-setup.md "Extras")
- `plugin` (object, checked by kitlib.pluginsource): where the plugin export of pushed kit main lives, and whether pm_watch refreshes it on its own (docs/plugin-source.md); older kit copies reject the key; set it only after every folder is migrated
  - `export_dir` (absolute path): the export root; unset: `%LOCALAPPDATA%\pm-kit\plugin`. Nothing needs setting for the default. It must lie outside the kit checkout and `agent_temp_sweep.dir` (`kitlib.pluginsource` `placement_errors`).
  - `auto_refresh` (true or false, default false): pm_watch starts `tools/plugin_refresh.py --apply` itself when kit main moves.
  - `remote` (a remote name, default `origin`): the kit checkout's remote whose `main` is exported.

## Moving the machine config folder

The machine config folder was renamed: `kitlib.config.user.DIR_NAME` is the new name, `kitlib.config.migration.OLD_DIR_NAME` the retired one. The kit moves no file; the owner does, once.

- **Which file is read:** `kitlib.config.user.locate` decides on the new folder's `config.json`, never on the folder alone (another writer may make the folder first). New file present: it is read, and the old one is ignored, never merged. New file absent: the old folder's file is read (the fallback). Neither: an empty config, named at the new folder.
- **Fail closed:** a new folder that is not a folder, a new `config.json` that is not a file, or a path that cannot be examined (an error other than not-found) is `BAD`: a `ConfigError` for every reader, never the fallback, and the guard denies every subagent write (docs/guard.md "State", User config). That includes the modules that open the file themselves through `user.readable_file`: `kitlib.pmstart`, `kitlib.coordinatorstart` and `kitlib.pmwatch` raise `ConfigError`, `kitlib.backloglint` `LintConfigError`, `optional/agenttemp/agent_temp_sweep.py` `SweepConfigError`; `tools/pm_watch.py` prints `machine config: <why>` and exits 2, `tools/backlog_lint.py` and the sweep exit 2 with the reason too, and the PM and coordinator session-start hooks print it as a check that failed.
- **Writers** write the new folder's file only (`user.writable_file`): `tools/pm_data.py init` (`kitlib.pmdata.init.write_machine`) refuses, before any write, while the config is still only in the old folder, since a new file holding one key would hide every other key.
- **The `config` line of `tools/preflight.py`** (`kitlib.preflight.configline`) says which file is read: the new file, `FALLBACK` (the old one), `WARN both` with `same content` or `DIFFERENT (keys: ...)`, `none`, or `BROKEN`. It names keys, never values: a file the strict reader refuses is `BROKEN <file>: bad key(s): <names>` (`user.failing_keys`), `not valid JSON`, `not a JSON object` or `a key written twice in one object`, never the reader's own message, which quotes the refused value.
- **Both present:** the new file decides alone, so the guard protects the new file's `ledger_path` only; an old file naming another ledger leaves that file unprotected (docs/guard.md "Known limits").
- **Other kit files in the folder:** the fallback covers `config.json` alone. The hub's token files live in the new folder only and are never read from the old one (`kitlib.hubsite.auth.folder`).
- **The owner's move:** write the new folder's `config.json` from the old one (a copy, plus any new keys), check that the preflight `config` line names the new file, move any file the config points into the old folder (and its path), then retire the old folder by moving it out of the home folder's `.claude-*` names: `tools/machine_setup.py` takes every other `.claude-*` folder for a Claude account folder.
- `machine_setup.py` skips both folder names (`kitlib.machinesetup.default_config_dirs`). The modules that use `OLD_DIR_NAME` are listed in tests/scrub/test_old_config_name.py, so retiring the fallback finds each.
