# `.kit.json`

A repository's kit configuration, read and checked whole by `kitlib.config.load` (`load_at` reads it at a revision: a landing reads the base branch's). The schema is `kitlib/config/schema.py`; tests/config/test_user_docs.py keeps the lists below equal to it.

- A section is an object or, except `repo`, `owner`, `hook`, `guard` and `modules`, a string naming a repo-relative JSON file that holds it.
- A section is read only from `.kit.json` or the file it names: no `tools/*.json` is read because it exists.
- An unknown section or key refuses, and so does a key written twice in one object. One error names every problem. A key starting `_`, or `comment`, is a note, allowed in every object.
- A key whose default is unset may be `null`. Unset means the tool that reads it decides.
- No environment variable overrides the file or the repository root.
- With no `gate` section at all the gate detects the stack and runs its usual test and lint steps, labelled `source: auto` (docs/gate.md "Auto-detected gate"); a `gate` section, even an empty one, replaces that whole.
- A `gate.steps` entry runs either `cmd` or `check`, a gate check the repo hook module provides in `GATE_CHECKS`; repo-specific gate settings go in `hook.options`. The step keys are `STEP` in `kitlib/config/schema.py`.
- `gate.kind` names the kind a gate run books; the lock books that kind's size (kitlib/lock/kinds.json). A size is a whole number of cpu slots 1..the layout's agent_cap (`medium` and `heavy` remain as aliases), set only by `gate.py --measure` and a reviewed commit. No `.kit.json` key sets a gate's core count. A retired kind name (`engine-test`, `engine-run`) still books, as the kind it names now (docs/lock-api.md "Snapshot", `kinds`).
- `tools/land.py` reads `main_branch` and `remote` from `repo`, `wait_s` and `batch_max` from `landing`, and the gate steps from `gate` (docs/gate.md, Landing).
- No old spelling is read: the hook module is the top-level `hook` (not `gate.hook`), and owner settings are `owner` (not `doc_budget` keys).
- `backlog.code_main` matters where the store's checkout is on a branch that shares no history with the code, as the kit's pmdata store is (its `repo.main_branch` is `pmdata`): there `backlog.py add --found-on <branch>` (`plan_add` in `kitlib/backlog/plan.py`) finds no merge base and stops with an ERROR naming the key, until the PM sets it to the code's main branch. In the PM data worktree itself an unset key takes the main checkout's `repo.main_branch`, and the code main is read on the main checkout's `repo.remote` (`code_config` in `kitlib/backlog/pull_branch.py`, used by `open_store` in `kitlib/backlog/opener.py`; docs/backlog.md "The fold-in guard"). `--found-on none` needs no base.
- Retired keys are still accepted and ignored, so an older install loads: `backlog` may still carry `retired_docs`, `rename_script` and `ledger_repo`, and `landing` may still carry `lanes` and `lock_file` (`RETIRED` in `kitlib/config/schema.py`); nothing reads them. A whole `ruling_cites` or `stubs` section is accepted the same way and dropped unchecked (`RETIRED_SECTIONS` there); any other unknown section still refuses.
- A repo lists its own heavy programs in `heavy.programs`; the kit defaults are general tools only (godot, blender, dotnet, gradle). For example `{"program": "run_tests.bat", "kind": "test", "why": "runs the engine test suite"}`, `{"program": "graph_build*", "kind": "graph-build", "why": "rebuilds the graph"}` and `{"program": "local_model*", "kind": "local-ai", "unless_args": ["--help"], "why": "loads a model onto the gpu"}`.

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
- `repo.tools_dir` (repo path, default "tools"): where repo tools and the hook module live
- `repo.push_block` (bool, default false): pre-push refuses a push to main without a gate-OK record; a delete or a forced push of main is refused whatever it says
- `owner.email` (email): author email of the owner's own commits; unset: the machine config's owner_email; neither: no owner commit is provable, so every owner-only change is refused
- `owner.agent_emails` (list of email, default []): author emails known to be agents', never the owner's
- `owner.paths` (list of string, default ["AGENTS.md", "CLAUDE.md", ".claude/", ".codex/", "docs/agent-profile.md", ".github/workflows/", ".kit.json", "tools/gate.json", "kitlib/gate/strict_defaults.py", "kitlib/backlog/roles.py", "hosts/claude/managed-settings.json"]): owner-only paths; an entry ending / locks a directory; replaces the default
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
- `backlog.cap` (int >= 1, default 5): most records `backlog.py next` offers and `pull` takes in order (`next --cap` overrides it for one call); every worktree reads the main checkout's value
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
- `gate.worktree_guard` (bool): refuse to gate in a main checkout
- `gate.warn_untracked` (bool): warn about untracked files
- `gate.fail` (regex or list of regex): log patterns that fail every step
- `gate.warn_lines` (regex or list of regex): log patterns that warn
- `gate.zero_tests` (regex or list of regex): log patterns meaning no test ran
- `gate.errors` ({match, excuse, expect}): error-line policy
- `gate.kind` (a kind name, default "test"): the kinds-table kind a gate run books (gate.py and gate.py land)
- `gate.smoke_steps` (list of a step name): steps a default (not --full) branch gate runs whatever changed; a landing takes such a gate's record for a tip once these steps passed in it (docs/gate.md "Landing"); unset: a landing needs a --full record
- `gate.docs_only` ({globs, exclude, steps}): a change whose every path is docs: the branch gate runs only its steps, and a landing takes that record (docs/gate.md "Docs-only changes"); unset: no change is docs-only
- `guard.data_roots` (list of {path, mode, why}, default []): folders besides their checkout and temp that agent sessions may write, each with its mode; read from the main checkout only (docs/guard.md "Write jail")
- `guard.role_policy` (bool, default false): hold roles to their role policy mode (docs/guard.md "Role policy"); off: no role is held, whatever its mode; read from the main checkout only
- `landing.log_dir` (repo path): where gate logs live
- `landing.wait_s` (int from 0 to 86400, default 21600): seconds tools/land.py waits for its turn (its machine-lock booking, then the landing lock) unless --wait says otherwise
- `landing.batch_max` (int from 1 to 20, default 3): most branches one tools/land.py landing merges and gates together
- `landing.self_review_max_lines` (int from 1 to 100000): most changed lines (added plus removed, git diff --numstat: the larger of <base>...<tip> and <main as fetched>...<tip>) a verdict the queueing coordinator reviewed itself may cover: tools/verdict.py add refuses a larger self-read, or one whose size cannot be told (docs/gate.md "Strict paths"); unset: no size check
- `landing.reuse_max_age_h` (int from 0 to 168, default 24): hours a pass stored in the gate cache serves a branch gate rerun or a landing; 0: no gate reads or stores a step result
- `landing.reuse_never` (list of string, default []): globs of paths whose change makes a gate (branch or landing) read and store no step result in the gate cache
- `landing.in_flight` ({wait_s, command}): how tools/new_worktree.py and tools/sync_base.py see a landing in flight before they fetch, and wait for it (unset: off; {}: the kit landing lock; wait_s unset: 100, inferred not measured; docs/new-worktree.md "Waiting for a landing in flight")
- `landing.shared_paths` (list of whole-path glob (a folder is "api/*", never "api/")): globs of the repo's shared code: a landing takes a default-mode (scoped) record of a tip that changes none of them, no strict path and no gate config file, without "gate".smoke_steps; a tip that changes one of those needs a --full record, whatever "gate".smoke_steps or "gate".docs_only say (docs/gate.md "Shared paths"); unset: a landing needs a --full record unless "gate".smoke_steps or "gate".docs_only take it
- `landing.gate_once` (bool, default false): a landing takes a default-mode (scoped) record of any tip that changes no strict path and no gate config file, shared paths included, and its merged gate runs --full (each step's full_cmd) once on the merged or batched tree; a docs-only merge stays docs-only (docs/gate.md "Gate once at landing"); off: "landing".shared_paths, "gate".smoke_steps and "gate".docs_only decide alone. It is the PM's operating rule, not an owner ruling: switch it off if more than 10% of 30 landings under it go red, or if the landing lane is over 50% busy for a week, both measured from the `.land.log` files; turning it off is an owner edit to `.kit.json`.
- `ledger.pre_commit_check` (bool, default false): githooks/pre-commit runs tools/ledger.py check --base HEAD before each commit that changes the ledger file or its standing digest (opt-in; docs/ledger.md "Pre-commit check")
- `machine_paths.allow` (list of {glob, line, hit, why, temporary}, default []): machine-path hits allowed
- `machine_paths.fixtures` (list of {glob, hits, line, why}, default []): test fixtures that name machine paths
- `machine_paths.known_binary` (map path -> string, default {}): binary file -> why it may hold paths
- `machine_paths.scan` (list of string, default ["*"]): globs scanned
- `machine_paths.fallback_vars` (list of string, default []): environment variables a machine path may fall back to
- `maint_lint.gate_time` ({prefix, bound_s, session}): the gate-time check of tools/maint_lint.py (docs/maint-lint.md "Gate time"): bound_s (1 to 86400, default 900) the seconds a gate step's median time over full gates may reach; prefix the id prefix of the record a plain run files for a step over it (unset: none is filed); session the session that record carries; unset: the check runs at 900 s and files nothing
- `modules.map` (repo path): the module map file: modules, messaging, allowed edges and ratchets (docs/module-map.md); --check refuses to run unless "owner".paths locks it; unset: no module map
- `modules.ceilings` (repo path): the ratchet ceilings file, {ratchet id: ceiling} (docs/module-map.md "Ratchets"); unset: every ceiling is 0
- `modules.page` (repo path): the architecture page generated from the map (docs/module-map.md "The architecture page"); --page --write writes it and --check fails when it does not match the map; unset: no page
- `pieces`: a table the kit passes through unchecked (a file path or an object)
- `preflight.engines` (list of string, default []): process-name prefixes listed as running engines
- `preflight.report_hooks` (list of "module:function", default []): module:function report hooks
- `preflight.traps` (string): where the traps are written down
- `preflight.udp_ports` (list of 2 int from 0 to 65535): [low, high] ports checked free
- `redfirst.runners` (list of {glob, kind, pythonpath, dir, project, csproj, args, lock}, default [{"glob": "tests/*test_*.py", "kind": "python"}]): which runner takes a test file, first match wins; none written: this default, then <tools_dir>/*test_*.py as python with the tools folder as pythonpath, unless tools_dir is . or that glob is listed (docs/redfirst.md); a runner's lock is the kinds-table kind the run books around its command (unset: the kind of the heavy-list entry the command matches)
- `redfirst.hold_wait` (int >= 0, default 60): seconds a held throwaway tree is re-checked before it is left
- `review_pack.coauthor` (bool, default false): list the branch's commits with no Co-Authored-By trailer, judged by the trailers rule; a failing one blocks
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

The machine's own settings, read by `kitlib.config.user` from the PM repository the PM repo pointer names ("The PM repo pointer"): the tracked `<PM repo>/.pm-kit/config.json` holds the policy keys and the git-ignored `local.json` beside it the machine-local and personal ones (`user.LOCAL_KEYS`; "Moving the machine config into the PM repo"). `python -c "from kitlib.config import user; print(user.config_file())"`, run in the kit, prints the path of the policy file read. Messages name the file read (`user.display()`), help text names `<PM repo>/.pm-kit/config.json`, and this page and the others write `<config-dir>` for that folder. No `.kit.json` key and no ledger environment variable overrides them. The pointer is machine-wide, so every session reads the same files whatever its USERPROFILE; the home folder (`~`: USERPROFILE on Windows, HOME elsewhere) decides only where a file left behind is looked for ("Moving the machine config folder"). `machine_lock` is read with the home set to the profile folder Windows itself reports (`kitlib.lock.site.load`, through `kitlib.lock.store.machine_home`). With no pointer and no file in the home folder the config is empty. The path keys below that say "absolute" are checked as absolute after reading; but `ledger_dir`, `ledger_path` and `pending_path` (`user.RELATIVE_KEYS`) may be written in the PM repo's `config.json` relative to the PM repo root, which `user.resolved` makes absolute before that check ("Moving the machine config into the PM repo"), or absolute.

A retired key is still accepted and ignored, so an older machine config loads: `ledger_refs`, `standing_delegation`, `daily_red_ruling` and `backlog_lint` (`RETIRED` in `kitlib/config/user.py`); nothing reads them. The daily full run's red record is always priced at 2 by an agent (docs/daily-full.md "Bisect"). Any other unknown key still refuses.

Each kit copy on the machine reads this one file with its own key list, so a key "older kit copies reject" refuses the whole file in every copy that does not know it. `python tools/hub.py setup --check` lists each copy (the plugin export folders, the pins and each account folder's install, `kitlib.config.copies`) with the keys of the file plus `hub` that it would refuse, and writes nothing. It reads each copy's `SCHEMA` and `RETIRED` from the text of its `kitlib/config/user.py` and checks top-level keys only.

- `owner_email` (email): the author email of the owner's own commits, for every repository whose .kit.json sets no "owner".email; unset there too: no commit is provable as the owner's, so every owner-only change is refused (docs/kit-json.md "With no file"); older kit copies reject the key
- `ledger_dir` (absolute path, or relative to the PM repo): the rulings ledger folder: one file `<ledger_dir>/<repo>.md` per `ledger_repos` name, which it needs; set here only (docs/ledger.md "Files"); older kit copies reject the key; set it only after every folder runs a kit that knows it
- `ledger_repos` (list of a lower-case repo name): the repos `ledger.py add --repo` writes; `pm` is the machine-wide one; a repository's name is its folder name lower-cased with `_` as `-` (`My_Tools-Repo` is `my-tools-repo`); with `ledger_dir`, each name once, and never `all`, `standing` or the frozen file's name; without it, the one-file ledger's repo columns besides `all`, and unset: any lower-case name (docs/ledger.md)
- `ledger_path` (absolute path, or relative to the PM repo): the one-file rulings ledger (R-nnnn ids); set here only; once `ledger_dir` is set it is frozen: read, never written; removed one release after the split
- `pending_path` (absolute path, or relative to the PM repo): the owner's pending-steps file tools/pending.py edits (docs/pending.md)
- `scrub_patterns` (list of regex): case-insensitive patterns for the install's own names (repositories, owner, accounts) that the shipped-text tests and tests/scrub/test_generic.py also refuse
- `guard_mcp_prefix` (string): the lock MCP tool-name prefix the guard denies owner-only tools under; unset: the kit default (docs/guard.md "Lock MCP")
- `guard_mcp_tools` (list of string): more full tool names the guard treats as the lock MCP's owner-only tools, as a PreToolUse payload names them (docs/guard.md "Lock MCP")
- `guard_agent_marks` (list of string): the session marks the guard treats as agent sessions for the kill-by-name, lock-state, kit-json, write-jail and git-stash-shared rules; it replaces the default list, so it names every mark it holds; unset: coordinator, deep-worker, consultant and pm (docs/guard.md "Session mark")
- `guard_mark_roots` (map mark -> list of {path, mode, why}): more write-jail roots for the main sessions of one launcher mark, as {mark: [{path, mode, why}]}; a root that is, holds or lies inside either machine config folder, the current or the retired one, is ignored unless it is read-only (docs/guard.md "Write jail")
- `guard_rules` (map rule -> deny|warn|off): the guard's rule modes, {rule: deny|warn|off}, over the kit defaults (kitlib.guard.modes.DEFAULTS); set here only, never in .kit.json (docs/guard.md "Rule modes"); older kit copies reject the key; set it only after every folder runs a kit that knows it
- `pm_data` (object, checked by kitlib.pmdata): the worktree of the PM data branch: {path, branch, remote} (docs/pm-data-branch.md "Layout")
- `hub` (object, checked by kitlib.hubsite.settings): the Action Hub site settings: port, tunnel_port, store, commit_every_s, push (unset: on only when store is unset and pm_data is set), backlog_refresh_s, access: {team, aud, emails}; unset: every default, and the site still runs; older kit copies reject the key; set it only after every folder runs a kit that knows it
- `pm_start` (object, checked by kitlib.pmstart): the PM session-start checklist settings (docs/pm-start.md "Config")
- `coordinator_start` (object, checked by kitlib.coordinatorstart): the coordinator session-start settings
  Its install values (`kitlib.coordinatorstart`; nothing in the code supplies them):
  - `repos_root` (path): the folder whose top-level folders are the repositories; the session's cwd picks the repo under it. Unset: every check that needs the coordinator's repo prints "could not check ...: repos_root not configured".
  - `landing_lock` (retired): the landing-lock line reads the kit's fixed `kit-landing.lock` in the git common dir (`LOCK_NAME` in `kitlib/records/landing.py`), shown for any repo under `repos_root`; a config still setting the key is ignored without a word.
- `pm_watch` (object, checked by kitlib.pmwatch): the PM watcher settings
- `launcher` (object, checked by kitlib.launcher): the session launcher settings: state folder, repos, account folders (docs/launcher.md "Settings")
- `machine_lock` (object, checked by kitlib.lock.site): the machine lock settings: build daemons at release, lock-window notices
  Read from the config under the Windows profile folder (on Linux, the profile from the password database), not under the environment's home (the paragraph above); under the kit's tests the read is refused (docs/test-tools.md "Home fence").
- `temp_dir` (absolute path): the folder the kit's own temp files go in (test run folders, gate logs), and the shared temp folder (gate fallback logs, the worktree sweep log, private index folders, comfy_gen's GPU self-lock and state) while its drive is there, made if missing; on Windows, as the test and gate folder, it is skipped when it is, holds or sits inside a session temp (`agent_temp_sweep` `dir`, or the user environment's AGENT_TEMP, TEMP or TMP); unset or skipped: the first of these clear of every session temp: `kit-temp`, then `kit-work`, at the root of the checkout's drive (not the system temp folder's drive), the system temp folder (%LOCALAPPDATA%\Temp; on Linux /tmp), `kit-work` beside it (docs/test-tools.md "Temp folders"); older kit copies reject the key
- `machine_path_folders` (list of a folder name): the drive-rooted folder names the machine-paths check refuses besides a user profile (docs/commit.md "machine_paths.py"); unset: none, so only drive-rooted user-profile paths are refused and tools/machine_paths.py prints a NOTE saying so; older kit copies reject the key; set it only after every folder runs a kit that knows it
- `agent_temp_sweep` (object, checked by optional/agenttemp/agent_temp_sweep.py): the agents' temp folder to sweep (`dir`), the age past which an unused entry goes (`max_age_days`, default 3), the entries it never removes (`keep`, relative paths such as `claude/<project>/<session>`) and whether it also sweeps the session folders under the user's own temp, HKCU\Environment TEMP, else TMP (`user_temp`, true or false, default false; older sweeps refuse the key); `tools/machine_setup.py` adds it when absent (docs/machine-setup.md "Extras")
- `dev_drive` (absolute path): the Dev Drive's root folder: the gate and landing result records go under it, in `<dev_drive>\pm-kit\kit-gate-result` (docs/gate.md "Result record"), and `delegated.py run --approval` runs an operation, and `delegated.py request` asks for one, only on paths under it and outside every checkout (docs/delegations.md "Hub approvals"), and the guard holds every agent write on it (docs/guard.md "Write jail", "Outer bound"); unset: a gate writes no result record, every landing refuses, every approval run and request refuses, and the outer bound of the guard is off, said at every session start; older kit copies reject the key; set it only after every folder runs a kit that knows it
- `plugin` (object, checked by kitlib.pluginsource): where the plugin export of pushed kit main lives, and whether pm_watch refreshes it on its own (docs/plugin-source.md); older kit copies reject the key; set it only after every folder is migrated
  - `export_dir` (absolute path): the export root; unset: `%LOCALAPPDATA%\pm-kit\plugin`; on Linux `<profile>/.local/share/pm-kit/plugin`, the profile from the password database, never `$HOME` or `$XDG_DATA_HOME` (`pluginsource.default_export_dir`). Nothing needs setting for the default. It must lie outside the kit checkout and `agent_temp_sweep.dir` (`kitlib.pluginsource` `placement_errors`).
  - `auto_refresh` (true or false, default false): pm_watch starts `tools/plugin_refresh.py --apply` itself when kit main moves.
  - `remote` (a remote name, default `origin`): the kit checkout's remote whose `main` is exported.

## Moving the machine config folder

The machine config was read from the home folder, first from the retired folder `kitlib.config.migration.OLD_DIR_NAME`, then from `kitlib.config.user.DIR_NAME`; the kit now reads it from the PM repository only ("Moving the machine config into the PM repo"). A `config.json` still in either home folder is an error, never read or merged: remove it. The auto-tasks state has moved too, to `<PM repo>/.pm-kit/auto-tasks/` (git-ignored, `kitlib.autotasks.tick`); one still in `~/.pm-kit/auto-tasks` stops the tick until it is moved.

- **Which file is read:** `kitlib.config.user.locate`. A good pointer: the PM repository's files; a home `config.json` beside them is `LEFTOVER` (`Location.leftover`, `DIR_NAME`'s first, then the retired folder's), a `ConfigError` naming its `Remove-Item` command. A home `config.json` while the PM repository has none, or with no pointer, is `UNMOVED`, naming the move; no pointer and no home file is an empty config (`NONE`).
- **Fail closed:** a `BAD` pointer, a PM `.pm-kit` that is not a folder, a file that is not a file, or a path that cannot be examined is `BAD`; `UNMOVED`, `LEFTOVER` and `BAD` are a `ConfigError` for every reader, never a fallback, and the guard denies every subagent write (docs/guard.md "State", User config). That includes the modules that open the file themselves through `user.readable_file`: `kitlib.pmstart`, `kitlib.coordinatorstart` and `kitlib.pmwatch` raise `ConfigError`, `optional/agenttemp/agent_temp_sweep.py` `SweepConfigError`; `tools/pm_watch.py` prints `machine config: <why>` and exits 2, the sweep exits 2 with the reason too.
- **Writers** write only the file `user.writable_file` names: the PM repository's `local.json` for a `LOCAL_KEYS` key, else its `config.json`. It refuses for `UNMOVED`, `BAD` and with no pointer, since it never makes a home file (machine setup's `owner_email` and `agent_temp_sweep` writes refuse then, naming `tools/machine_setup.py --pm-repo`). `tools/pm_data.py init` writes none: `pm_data` is derived from the pointer (docs/pm-data-branch.md "Making the branch").
- **The `config` line of `tools/preflight.py`** (`kitlib.preflight.configline`) names the files read, or `BROKEN` with the reason (a leftover among them). It names keys, never values.
- **Session start** (`kitlib.pmstart.configcheck`): the PM checklist, the coordinator context and the host session-start checks (`kitlib.hoststart.shared_checks`, pm and coordinator) print the machine config lines right after the standing lines, each starting `machine config: `, and nothing when all is well:
  - the leftover: `machine config: a copy is still in the home folder (<file>); the kit reads only <file read> and stops until it is removed: remove it (Remove-Item -LiteralPath '<file>')`, a `'` in the path written `''`;
  - the auto-tasks state still in the home folder (`kitlib.autotasks.tick` `home_problem`): `machine config: the auto-tasks state moved to the PM repository (<new>); ... move it (Copy-Item -LiteralPath '<old>' -Destination '<new>' -Recurse, then, once the copy is there, Remove-Item -LiteralPath '<old>' -Recurse)`, or only its `Remove-Item` when both folders exist, or, when the PM repository's `.pm-kit` is not there yet, a line to make it first (`tools/config_move.py`);
  - git in the PM repository does not ignore `auto-tasks/` (`kitlib.autotasks.tick` `unignored`): `machine config: git does not ignore the auto-tasks state folder in <PM repo>: add the line auto-tasks/ to <its .gitignore> and commit it`;
  - no pointer: `machine config: no PM repo pointer: set it (tools/machine_setup.py --pm-repo, ...)`; `UNMOVED` or `BAD`: `machine config: cannot be read: <why>`;
  - the integrity line, for the PM repository's `config.json`: `was never committed` (untracked, or no commit; no file at all is no line), `has uncommitted changes` (with its `git -C <PM repo> diff` command), or `was last committed by someone other than the owner` (with its `git -C <PM repo> log -1` command). The last author is compared with the PM repository's owner rules (`kitlib.config.load` `owner_rules`); the line never names an address. With no owner identity it says it cannot check. A git call that fails is a `could not check machine_config` line.
  A coordinator with no `coordinator_start` config still gets them, after the skip line; so does a PM on the claude host with no `pm_start` config (`hosts/claude/adapter/host.py` `start_text`, through `configcheck.appended`; tests/host/test_claude_start.py).
- **Kit copies:** a copy whose `kitlib/config/user.py` predates the pointer reader still reads the home folder's file, so `python tools/hub.py setup --check` lists it as `reads the home folder: restart its sessions or prune it`, never as knowing a key; one in use makes the check not ready (`kitlib.config.copies.pre_pointer`).
- **The owner's step:** once the PM repository's files are committed and the preflight `config` line names them, remove each leftover `config.json` and move the auto-tasks state with the commands the session-start lines give.
- `kitlib.config.user.config_dir` (= `pm_config_dir`) is the PM repository's `.pm-kit` when the state is OK, else None. The agent-temp sweep and machine setup, which only keep clear of the config, use `keep_clear_dirs`: the PM folder for OK and leftover, the home folder still holding the file for unmoved and leftover, for a bad one the PM folder when the pointer is good and nothing when the pointer is bad, and nothing for no config.
- `machine_setup.py` skips both folder names (`kitlib.machinesetup.default_config_dirs`). The modules that use `OLD_DIR_NAME` are listed in tests/scrub/test_old_config_name.py, so retiring the name finds each.

## The PM repo pointer

One machine-wide registry value names the PM repository: `HKLM\SOFTWARE\pm-kit`, value `pm_repo`, a REG_SZ (`kitlib.config.pointer`). It lives under HKLM, so only an administrator can set it, and an agent cannot point the kit at another PM repository. The kit reads the machine config through it ("Machine config keys"); preflight's `pointer` line reports it (docs/preflight.md).

- **Read:** `kitlib.config.pointer.read`, straight from the registry in the 64-bit view (`KEY_WOW64_64KEY`), never from the environment. No key or no value: `NONE`. A value that cannot be read, is not a REG_SZ or is empty: `BAD`.
- **The value:** the absolute path of the PM repository's main or data work tree, never a `pm/<slug>` worktree (`kitlib.config.pointer.check`). It must be an existing folder and the top of a git work tree. When the repository's git common dir holds `kit-pm-data.json` (`kitlib.pmdata`), the work tree must be on the recorded data branch; with no record it must be the main checkout. Anything else is `BAD`, with the reason.
- **On Linux** there is no registry: the value is one line of UTF-8 text in `/etc/pm-kit/pointer`, which only an administrator can write, else in the user's `<profile>/.config/pm-kit/pointer`, the profile folder from the password database, never `$HOME` or `$XDG_CONFIG_HOME` (`kitlib.platform.linux.paths.read_pointer`). Neither file: `NONE`. The file in force that cannot be read, is not UTF-8, is empty or holds more than one line: `BAD`; a bad machine file is the answer, and the user's file is then not read. The value is an absolute path with no drive. A user with no password-database entry has no profile folder, so without the machine file the pointer is `BAD` and the machine lock's store cannot be opened. The user's file is writable by that user, so it does not keep an agent from pointing the kit elsewhere. The reader is all the code has: the plan has machine setup write the machine file (docs/linux-support-plan.md "6. Install and machine setup on a fresh Linux box").
- **Under the kit's tests** (`kitlib.config.user.fenced`) the registry (or a pointer file) is never read: the value is the environment's `KIT_PM_REPO_FENCE`, and with none there is no pointer.
- **Set it** from an administrator terminal: `machine_setup.py --pm-repo <PM repo>`, a dry run first (docs/machine-setup.md "PM repo pointer").

## Moving the machine config into the PM repo

A one-shot copy of the machine config from the home folder into `<PM repo>/.pm-kit/`, the PM repository the pointer names (`tools/config_move.py`, `kitlib.config.move`). With no flag it is a dry run that writes nothing. `--apply` copies and checks the copy. It never deletes, commits or pushes; it prints the owner's commands for those. Exit codes: 0 OK, 1 REFUSED, 2 USAGE.

- **Split:** the policy keys (`move.POLICY`) go to the tracked `config.json`. The local and personal keys (`move.LOCAL`: `owner_email`, `scrub_patterns`, `launcher`, `hub`, `dev_drive`) and the notes go to the git-ignored `local.json`. `ledger_dir`, `ledger_path` and `pending_path` (`move.RELATIVE_KEYS`) are written relative to the PM repo when they lie inside it.
- **Dropped:** `ledger_refs`, `standing_delegation`, `daily_red_ruling` and `backlog_lint` (all retired), and a `pm_data` that names the pointer's work tree, branch and remote; a `pm_data` that does not is a refusal. The branch and remote are checked against the repo's `kit-pm-data.json` record; without a record, the branch must be the one the pointer is on and the remote one the PM repo has.
- **Copied byte for byte:** `owner-delegations.json` (tracked), `managed-settings.json` and `hub/*` (git-ignored). The move writes `.pm-kit/.gitignore` (`move.GITIGNORE`) first, and checks with `git check-ignore` that git ignores every private file before it writes any of them. Each file is written through a temp file beside it (`*.move-tmp`, which the ignore file also covers, as it covers `auto-tasks/`, the tick's state, which the move never copies), removed if the write fails. A write that fails part-way stops the move with a refusal naming the file; the files already written stay, and a rerun resumes, since they show as `same`. `*.bak*` and `hub_backlog_state*.json` are never copied; they are listed as `delete`. A file the move does not know is a `NOTE` and is not copied.
- **Refusals:** no pointer, or a `BAD` one; no machine config; a config the schema refuses; a key the move does not know (`NOTE` is for files only); a target that exists and differs; a `.pm-kit/` with uncommitted changes, unless every changed path is one the move would write and is already byte-equal to it. Also refused: a tracked value (a policy key, or `owner-delegations.json`) holding an email address (`owner_email`'s, or any address) or the home or Windows profile folder. A tracked value (a policy key, or `owner-delegations.json`) that matches a `scrub_patterns` entry gives a `warn` line instead, and the exit code is unchanged. Messages name the key and the kind of detail, never the value or the pattern.
- **Verify:** after `--apply`, both files parse and each key is in its own file. The two together, with relative paths resolved, pass the machine config schema. Every file is byte-equal to its source, and git ignores every private file.
- **The owner's steps** (printed): check and commit the tracked files; add the `.pm-kit/` ignore lines to the PM repo's top-level `.gitignore` as a backstop. Once the kit reads the PM repo's config, remove each home `config.json` (the kit stops until then) and move `~/.pm-kit/auto-tasks`.
