# Installing the kit

Two steps: set the machine up once, then work in any repository, with nothing of the kit written into it (plan decision 27, docs/multi-host-plan.md section 7). Held for the joint review: no repository runs the kit yet, and machine setup has not run on a real machine. The zero repo setup cycle below is proven by a test (`tests/zerosetup/test_cycle.py`, "What first use writes").

A repository never holds a copy of the kit, and by default nothing else either: no stubs, no
`.kit.json`, no ignore lines. Skills, roles and the default profile run every kit tool from the
kit's folder, as `python <kit>/tools/<name>.py -C <worktree>` (decision 21 in
docs/multi-host-plan.md section 7; the kit header names `<kit>`, docs/content.md "Kit header").
A repository that wants its kit settings committed uses the committed setup ("Committed setup
(--repo)"); stubs, small files in the repository's tools folder that run the kit's tools, are
opt-in there (`--stubs`) or by `kit_stubs.py write` ("Stubs: tools/kit_stubs.py").

## Steps

0. **A Dev Drive** (optional, recommended; Windows only, does not apply on other hosts). Running
   many agents and test suites makes many small file writes (worktrees, test temp files, gate
   logs, git objects), and Defender's real-time scan of each one can eat a large share of the
   CPU, as it did on the machine the kit was built on until the move to a Dev Drive cut that
   load. A Dev Drive is a ReFS volume built for source repositories, package caches and build
   output, where Defender runs in performance mode: it defers each scan until after the file
   open completes instead of scanning in the open path. Put the kit, the repositories and their
   worktrees on it; the kit's own temp files (test runs, gate logs) then follow the checkout onto
   that drive (`kitlib/config/user.py` `work_temp`). From Microsoft's Dev Drive guide
   (learn.microsoft.com/en-us/windows/dev-drive/):
   - Needs Windows 11 build 10.0.22621.2338 or later, local administrator rights, and at least
     50 GB. The C: drive cannot be one, and an existing volume cannot be converted.
   - Create it in Settings > System > Storage > Advanced storage settings > Disks & volumes >
     Create dev drive, as a new VHD, by resizing an existing volume, or on unallocated space.
   - Defender runs in performance mode on it when the drive is trusted (it is, from creation)
     and real-time protection is on. `fsutil devdrv query <drive>:` shows whether it is
     trusted; `fsutil devdrv trust <drive>:`, run as administrator, trusts it. Windows Security >
     Virus & threat protection settings > Manage settings > Dev Drive protection > See volumes
     shows whether performance mode itself is on
     (learn.microsoft.com/en-us/defender-endpoint/microsoft-defender-endpoint-antivirus-performance-mode).
   - Then clone the kit and the repositories into it.

**Windows: apply the token-leak fix** (optional, recommended). It stops process launches slowing
down the longer the machine stays up: docs/windows-token-leak.md.

1. **Machine setup, once per machine** (docs/machine-setup.md), from the kit's main checkout,
   in a terminal:

   ```
   powershell -NoProfile -File installer\machine-setup.ps1 [-OwnerEmail <your commit email>] [-PmSettings <PM repo>\.claude\settings.local.json]
   ```

   Read the dry run, then run it again with `-Apply`. It adds the guard's entry to each Claude
   account's `settings.json`, installs the kit plugin per account from the export of the kit's
   pushed main, stores the owner's email in the machine config (`-OwnerEmail`; without it the
   `-Apply` run asks in the console, offering your `git config user.email`, which is never
   stored unconfirmed) and makes the agents' temp folder. It writes nothing into any
   repository but the `-PmSettings` file, when you name one (a local settings file kept out of
   commits). A machine that ran an older kit clone uses `-OldKit <that clone>` instead
   (docs/machine-setup.md). Run it yourself: the guard refuses `-Apply` to agent sessions, since
   it stores the owner's email (docs/guard.md "Owner machine setup").
2. **A repository** needs nothing installed: any git checkout with an `origin` remote whose
   default branch is pushed (`origin/HEAD` names main) works as it is. `<kit>/tools/new_worktree.py`
   fetches `origin` (it refuses if the fetch fails), and `<kit>/tools/preflight.py` does only with
   `--fetch` (it never refuses for a missing remote). A new repository: `git init -b main`,
   `git remote add origin <url>`, a commit, `git push -u origin main`. With no `.kit.json` the
   kit works out its settings (docs/kit-json.md "With no file"): the main branch from
   `origin/HEAD`, the repository's name from its remote or folder, the owner from the machine
   config, the safe protections on (no force push and no deletion of main). The gate detects the
   stack and runs its usual test and lint steps, labelled `source: auto` (docs/gate.md
   "Auto-detected gate"). The doc index and the repo profile are opt-in (docs/doc-tools.md,
   docs/agent-profile.md).
3. **Overrides, when the defaults are not enough:** a `.kit.json` with only the keys that differ
   (docs/kit-json.md), committed under the owner's identity; a gate section that lists the
   repository's checks as `gate.steps` (a start per language:
   `templates/stacks/<stack>/tools/gate.json`); where landers edit the main checkout,
   `roles.lander.edit_main_checkout: true`. `<kit>/tools/gate.py` books the machine lock itself:
   do not wrap it in `machine_lock.py run`: the gate would then run as a nested lock run, booking
   twice. The committed setup below writes a starting `.kit.json` and the rest in one step.
4. **Later:** turn on `repo.push_block` only after the repository has landed once through
   `<kit>/tools/land.py` or `gate.py land` (docs/go-live.md "Universal gate").

### What first use writes

The first time the kit works in a repository (the PM's start, `tools/new_worktree.py`), the
first-use prep (`kitlib/git/prep.py`; docs/new-worktree.md "First-use prep") writes local git
settings only, never a tracked or untracked file, and nothing is committed:

- `.git/info/exclude` (in the git common folder, so every worktree shares it): one anchored line
  per agent worktree folder, `/.claude/worktrees/` and `/.codex/worktrees/` (each host's
  `worktrees_directory` in `hosts.json`, and the repository's own `worktrees_dir` when its
  `.kit.json` sets one). The worktrees then never show in the main checkout's `git status`, and
  a worktree still pushes to its remote branch.
- `core.hooksPath` (local) = `<kit>/githooks`, the kit's pre-push and pre-commit hooks, only when
  no hooks path is set at any level and the repository's own `.git/hooks` holds no hook. An
  existing value is kept.

Each step is idempotent and a no-op when there is nothing to do. `tests/zerosetup/test_cycle.py`
proves the whole cycle on a fresh clone of an unrelated repository with nothing of the kit in it:
machine setup's owner email, a PM's new worktree (the prep runs), a worker's commit through
`commit.py` and push of its branch, the auto-detected gate (the clone's own tests run, and a red
branch is held back), and `gate.py land` merging, gating and pushing to the remote. Afterwards the
remote's history past the clone holds the worker's files and nothing else, the main checkout's
`git status` is clean, and `info/exclude` and `core.hooksPath` are set locally. It runs on the
fake git with the machine lock replaced by plain child processes (any platform), and on Windows
also on real git, with the kit's hooks run, and the lock's job objects on a scratch store; its
docstring lists what each stands in for.

## Committed setup (--repo)

For a repository that wants its kit settings committed (the setup every repository needed before
decision 27): `python <kit>/tools/machine_setup.py --repo <checkout> ...` (`kitlib/machinesetup/repo.py`),
or its PowerShell front end `installer/new-repo.ps1`, which passes its parameters through
(`-Target` is `--repo`, `-OwnerEmail` `--owner-email`, and so on). Nothing in the kit needs it.

1. **The repository** must be a git checkout's top level, with an `origin` remote and a first
   commit (step 2 above); the setup warns, and prints the commands, when it finds no `origin`.
2. **Dry run**, from the kit checkout:

   ```
   python <kit>/tools/machine_setup.py --repo <checkout> --owner-email <owner> --agent-email <agents> [--main-branch main] [--repo-name <name>] [--stack python] [--stubs]
   powershell -NoProfile -File installer\new-repo.ps1 -Target <checkout> -OwnerEmail <owner> -AgentEmail <agents> [-MainBranch main] [-RepoName <name>] [-Stack python] [-Stubs]
   ```

   It prints what it would write: `.kit.json` (`"repo".main_branch`, `"repo".name`,
   `"backlog".store` `"ref"`, `"owner".email`, `"owner".agent_emails`), `.gitignore` and
   `.gitattributes`, a starter `docs/INDEX.md`, with
   `--stack` the stack's `tools/gate.json`, and with `--stubs` only the stubs (by
   `kitlib.stubs`, as `kit_stubs.py write -C <target>`; without it the dry run says `stubs: none`),
   and under `memory:` the `autoMemoryDirectory` key
   (`<the main checkout's drive>/Agent-memory/<repo folder name>`, `kitlib/stubs/memory.py`): added
   to `.claude/settings.local.json` only when neither that file nor `.claude/settings.json` has it,
   kept when either names any absolute folder, refused when the local file is not JSON it can edit or
   the key is not an absolute folder. The key is a machine path, so it goes in the git-ignored local
   file (the `.gitignore` lines above name it) and there is nothing to commit. It never makes the folder.
   `"repo".name` is the repository's rulings-ledger column (docs/backlog.md "Owner gates"): `--repo-name`,
   else the target folder's name lower-cased; either must match `[a-z0-9][a-z0-9-]*` or it
   refuses (exit 2). Both emails are required and must differ, or an agent's commit could pass
   as the owner's.
   - `.gitignore` and `.gitattributes` start from `templates/common` (`.claude/worktrees/` and
     `.codex/worktrees/`, which the first-use prep also puts in `info/exclude`) plus the stack's
     `gitignore` and `gitattributes` lists. A file already there is never overwritten: only the
     lines it lacks are appended, under `# Added by the kit installer`, and the dry run lists them.
   - `docs/INDEX.md` is written only when absent, and only by this setup: it opts the repository
     into the doc index check (decision 26). With an index file every tracked `.md` must be named
     on an entry line of it; the starter names only itself, so the dry run and `--apply` list
     the target's tracked `.md` files it does not name (under "add these to docs/INDEX.md"; advice
     only, never a refusal), for you to add. With no index file the check (run by preflight,
     commit and review_pack) is skipped and says so.
   - `--stack dotnet|godot|java|python` copies `templates/stacks/<stack>/tools/gate.json` to
     `tools/gate.json` (refused if one is there) and adds `"gate": "tools/gate.json"` to
     `.kit.json`, and copies the stack's other `tools/` files (dotnet: `tools/dotnet_each.py`; godot:
     `tools/godot.cmd`; python: `tools/js_tests.py`; java has none), keeping any that is there already. It also installs the stack's
     `.claude/skills/**` (`kitlib.stackskill`, as `tools/stack_skill.py install`; only Godot has one, `godot-render-check`): each `.md` has `{kit_root}`
     replaced by the kit's absolute path with forward slashes (the template quotes it, so a space
     is safe), a file already there is kept, and a kit root it cannot write (no
     `tools/machine_lock.py` under it, or a double quote, brace, `$` or backtick in the path) refuses
     the whole run before anything is written, as does a stack the kit has no folder for, and, for
     the PowerShell front end, no python on PATH. Edit the gate's globs and test command to the project.
     The python gate runs
     `unittest discover -s tests` and needs one test: its pass check wants a `Ran N tests` line
     with N of 1 or more, so a run of none fails on every Python version. It runs without
     `-t .`, so test modules import as top-level names: a `tests/__init__.py` layout with
     relative imports breaks (use absolute imports, or change the command).
     Without `--stack` there is no gate section and `gate.py` detects the stack (step 2 above):
     the closing text prints the copy-and-point step that replaces it, or write `gate.steps`
     yourself (docs/kit-json.md).
3. **Apply:** run it again with `--apply` (`-Apply`), then commit what it wrote. `.kit.json` and
   `tools/gate.json` are owner-only paths: commit them under the owner's identity, the rest
   (`.gitignore`, `.gitattributes`, `docs/INDEX.md`, and with `--stubs` the stubs) as usual. It
   refuses before writing anything when `.kit.json` or `tools/gate.json` exists, or, with
   `--stubs`, when a file that is not a stub sits where a stub goes. A write that fails (a read-only file, a full disk) stops it with
   `new-repo: FAILED`, the files already written, and exit 1. Exit codes: 0 done or dry run;
   1 refused or a write failed; 2 usage. Tests: `tests/machinesetup/test_repo.py` (any platform)
   and `tests/stubs/test_new_repo.py` (the PowerShell front end), against throwaway repositories only.
   - **PM data on a private branch** (optional; docs/pm-data-branch.md): add
     `--pm-data <folder> [--private-remote <url>]` (`-PmData`, `-PrivateRemote`). After the files above are written, it
     runs `python <kit>/tools/pm_data.py -C <kit> init --path <folder> [--url <url>]`: the data
     branch, its locked worktree at `<folder>` and the private remote belong to the kit's own
     repository, not the target's, and `<kit>` must be the kit's main checkout (`init` refuses a
     linked worktree). The dry run prints that command and runs nothing. `--private-remote`
     without `--pm-data` is a usage error (2). It prints `pm_data.py`'s lines and exits
     with its code: 0 done; 1 refused (nothing changed in the kit); 2 a configuration it cannot
     read; 3 made locally, the push failed (its lines say how to recover); 4 stopped part-way
     (rerun with `--adopt`); 5 crashed (what it made is not known; rerun with `--adopt` to
     check); 130 Ctrl+C. A Python traceback (exit 1) is also reported as a crash, not as a
     refusal. On a non-zero code the target's files stay written and it prints the
     command to run again on its own, quoted to paste into PowerShell. Its lines are shown as
     they come, but Ctrl+C may end the setup before its summary: then run
     `pm_data.py init --adopt` with the same `--path` to check what was made. A trailing `\` or
     `/` on `--pm-data` or `--private-remote` is dropped (a drive root is kept). The PowerShell
     front end takes a relative `-Target` or `-PmData` from the PowerShell location, and refuses
     a `"` in either (usage, 2). A repository already set up (its `.kit.json` makes the setup
     refuse) runs that command directly.
4. **An existing repository with its own copies of the tools** skips the setup. It needs a
   `.kit.json` only when it wants more than the defaults (docs/kit-json.md), and one that
   writes backlog records names its store there (`"backlog": {"store": "ref"}` or a folder:
   the default store alone is read-only, docs/backlog.md "Stores"); its
   own copies are then removed, since agents run the kit's tools from `<kit>`. A repository that
   wants stubs in their place instead runs
   `python <kit>/tools/kit_stubs.py write -C <repo> --replace`, then a commit.
5. **Stubs only:** `python <kit>/tools/kit_stubs.py check -C <repo>` exits 0.
6. **Git hooks:** the first-use prep sets them ("What first use writes"); by hand,
   `git -C <repo> config core.hooksPath <kit>/githooks`. This installs `pre-push` and
   `pre-commit`; `pre-commit` is inert unless the repository opts in (docs/ledger.md "Pre-commit check").

### What the committed setup does not install

- The stack's other files: `.github/workflows/{quick,full}.yml` (owner-only paths; copy them by
  hand). The stack's `tools/` files (see above) and its skills (only Godot has one:
  `.claude/skills/godot-render-check/SKILL.md`, with the kit path written in) are installed.
- `templates/common/AGENTS.md` (with `templates/common/CLAUDE.md`, the one line `@AGENTS.md`) and the two profile templates (`agent-profile.TEMPLATE.md`,
  `coordinator-profile.TEMPLATE.md`): the repository's rules file and profiles are drafted from
  them with the `<PLACEHOLDER>` slots filled (docs/shipping.md "Stack templates"). Both profiles
  are optional: with no agent profile, every role reads the kit's `content/default-profile.md`
  plus the repository's README and instructions file (docs/agent-profile.md).
- `.claude/settings.json`: the kit ships none (docs/go-live.md "Joint-review questions"); the
  setup adds only its `autoMemoryDirectory` key, to the ignored `.claude/settings.local.json` (step 2).
- `backlog/.gitkeep` is not needed (the setup's `.kit.json` names the ref store `refs/backlog/main`), and
  `.github/workflows/instructions.yml` is not shipped: the stack workflows do not mention it.
- Anything outside the repository: the ledger, the lock and the user config are per machine.

### Tools without a stub

Every kit tool runs by the kit's path, `python <kit>/tools/<name>.py`; one that acts on a
repository checkout takes `-C <checkout>` (tests/content/test_skill_commands.py holds the skills
and roles to that). For a repository that opts into stubs, the stubs cover the tools that act on
a checkout, named by `-C` or the current directory (`kitlib.stubs.SHARED`), including `land.py`,
`ledger.py`, `redfirst.py`, `trailers.py` and `daily_full.py` (which exits "off" unless
`.kit.json` opts in, docs/daily-full.md). Every `tools/*.py` command is in exactly one of
`SHARED` and `KIT_ONLY` (`kitlib.stubs`). The test `Partition` in `tests/stubs/test_kit_stubs.py`
fails when a new tool is in neither; the tool's author picks the list. The `KIT_ONLY` tools never
get a stub, because they act on the machine, the owner's files or the kit itself:

- `machine_lock.py`: one lock per machine, so it runs from the machine's lock install, never a
  repository's. `run` needs `--repo <name> --slug <slug>` (and `--kind`); `status` needs nothing.
  A repository's own commands use it only through a gate (booked by `gate.py`) or a wrapped heavy
  run.
- `kit_tests.py` (the kit's own suites), `lock_release_check.py` (its `-C` is the kit's release
  checkout), `backlog_lint.py`, `agent_roster.py`, `agent_view.py` (its `-C` names the repository
  it describes, but what it shows is the kit's roles): run by the kit's maintainers or the PM
  (docs/test-tools.md, docs/lock-release.md, docs/backlog-lint.md, docs/ROSTER.md, docs/content.md).
- `launch.py`, `machine_setup.py`, `pm_data.py`, `pm_watch.py`, `hub.py`, `hub_mirror.py`, `pending.py`,
  `worktree_sweep_task.py`: the owner's and the PM's, on the machine config and the machine.
- `no_window.py`: the carrier a scheduled task runs under `pythonw` so that no console window
  opens (docs/daily-full.md "The schedule").

A stub bakes in the kit it was written from, so one written from a kit worktree runs that
worktree and is stale on main (`check` says `runs the kit at ...`). At session start the
coordinator's and the PM's checklists print one `stubs (<repo>): N missing/stale: ...` line, with
the `kit_stubs.py write -C <repo>` command, for a repository that uses stubs (its `.kit.json`
sets `"stubs".tools`, or it holds one) and whose stubs are not current; a repository with none
gets no line (docs/pm-start.md).

### Plugin install (local marketplace)

Each account folder installs the plugin from the kit's export of pushed main, not from the
checkout (docs/plugin-source.md). `python tools/plugin_refresh.py --migrate --apply` adds the
export root as the marketplace `pm-kit` and installs `pm-kit@pm-kit` in every account folder; after
each kit landing `python tools/plugin_refresh.py --apply` refreshes them. Each is a dry run without
`--apply`. Migrate one folder first (`--folder DIR`, repeatable), check a session there, then the
rest; `--apply` refuses a folder that is not migrated (docs/plugin-source.md "Migrating"). The
exact `claude plugin` subcommands and flags these run, and the claim that an old session's MCP
server holds files open in the folder it loaded, are inferences (docs/plugin-source.md "Open
questions"). Adding the checkout itself as a marketplace (its `.claude-plugin/marketplace.json` lists
the plugin with `"source": "./"`) is the old way, which `--migrate` replaces. Whether Claude Code loads a relative-path plugin of a local marketplace in
place or copies it into its plugin cache is not settled: the plugins reference
(https://code.claude.com/docs/en/plugins/loading, "In-place and copied plugins") says in place, and
an install from the checkout was observed copied into the cache (docs/plugin-source.md "Open
questions"). Either way `${CLAUDE_PLUGIN_ROOT}` holds pushed kit main, not the live checkout:
the skills and agents write every kit path (every tool, `kitlib/`, `optional/`, the kit's docs)
as `<kit>/<path>` (tests/skills and tests/content/test_skill_commands.py check it); the kit
header hook tells each session and subagent the folder `<kit>` stands for (docs/content.md "Kit
header"). Agent and skill text gets no `${CLAUDE_PLUGIN_ROOT}` substitution, so neither uses it.
Source: https://code.claude.com/docs/en/plugin-marketplaces.
`claude plugin validate` passes with warnings only (no author field; CLAUDE.md is not loaded) and
reports no reserved name.

The plugin root is the whole kit (export), and `.claude-plugin/plugin.json` stays at its root: the
hooks and the MCP server run kitlib and tools beside them. plugin.json, marketplace.json and the hooks
file `hosts/claude/shell/hooks/hooks.json` (named by `"hooks"` in plugin.json) are rendered from
`kit.json` and content/ (docs/content.md "Manifests"). The plugin carries two hooks, each run in exec
form as `pythonw` with the kit's one hook entry, `${CLAUDE_PLUGIN_ROOT}/boot/hook.py claude <event>`:
the kit header (on SessionStart and SubagentStart; whether a plugin may register SubagentStart is
still to be checked live, docs/content.md "Kit header") and the coordinator SessionStart hook
(`boot/hook.py claude SessionStart coordinator`). Each account's settings already run the
coordinator hook, so the owner removes the settings SessionStart entry in the same
step as the install, or it runs twice (`tools/machine_setup.py` notes it, docs/machine-setup.md
"Settings"). The guard (`boot/hook.py claude PreToolUse`) stays in each account's settings and is
never in the plugin (docs/guard.md "Go-live"); the PM checklist (`boot/hook.py claude SessionStart
pm`) stays in the PM repo's settings.

Machine setup (`machine-setup.ps1 -Apply`, its default mode) does the same migrate for each account folder
whose plugin is not enabled from the export: it makes the export, then runs the migrate steps for
that folder through the same code (docs/machine-setup.md "Default mode"). The CLI also
rewrites other keys of that `settings.json`, so the step backs the file up first.

### After the plugin install: remove the per-item junctions

An account set up by `machine_setup.py` has per-item junctions under `~/.claude` that load the same
skills, hooks and agents the plugin now loads. Remove them in stages, in this order.

**Stage 1.** At install, remove the kit's `~/.claude/skills/*` junctions, or each skill appears twice.
Only the 12 kit skill names are touched (the directories under `content/skills`); any other link stays.
`cmd /c rmdir` removes the link only, never the target:

```powershell
$kit = 'backlog-ops','branch-review','branch-sync','coordinator-dispatch','doc-claims-discipline','flaky-test-triage','land-branch','machine-lock-run','mutation-testing','session-handoff','shell-and-worktree-traps','subagent-dispatch'
Get-ChildItem "$env:USERPROFILE\.claude\skills" -Force |
  Where-Object { $_.LinkType -eq 'Junction' -and $_.Name -in $kit } |
  ForEach-Object { cmd /c rmdir "$($_.FullName)" }
Get-ChildItem "$env:USERPROFILE\.claude\skills" -Force | Where-Object { $_.LinkType -and $_.Name -in $kit }
```

The last command must print nothing. Then `ListSkills` shows each of the 12 names once.

**Stage 1b.** Gated on the KIT-AGENTS-IN-PLUGIN rename landing; do not do this before.

Remove ~/.claude/agents only after the plugin providing pm-kit agents is installed and the launcher uses pm-kit:coordinator.

```powershell
cmd /c rmdir "$env:USERPROFILE\.claude\agents"
```

**Stage 2.** Remove the `~/.claude/hooks` junction only after the plugin's SessionStart hook is live and
`settings.json` no longer calls a guard through `~/.claude/hooks`. Check by searching
`$env:USERPROFILE\.claude\settings.json` for `\.claude\\hooks`: it must have no hit.

**Stage 3.** The `tools/*` and `knowledge/*` junctions stay for now: `~/.claude/tools/machine_lock` is the live
lock path, and `machine_setup.py --apply` recreates those links. Record KIT-JUNCTIONS-TOOLS-KNOWLEDGE
retires them.

`<kit>/tools/backlog.py -C <worktree> add` needs at least one `--scope <path>` (`none` for a record that touches no
tracked file); without one it refuses (docs/backlog.md). It also needs `--found-on <branch>` (or
`--found-on none` when no branch found it) and `--why-not-folded "<reason>"` (docs/backlog.md
"The fold-in guard").

The rulings ledger is once per machine: set `ledger_path` (and optionally `ledger_repos`) in
the machine config (docs/kit-json.md "Machine config keys"). The first `<kit>/tools/ledger.py add` creates the file from
`templates/common/LEDGER.md` (docs/ledger.md "A new ledger").

## Stubs: tools/kit_stubs.py

```
python <kit>/tools/kit_stubs.py write [-C DIR] [--tools a,b,...] [--replace] [--dry-run]
python <kit>/tools/kit_stubs.py check [-C DIR] [--tools a,b,...]
python <kit>/tools/kit_stubs.py memory [-C DIR] [--apply]
```

`memory` is not about stubs: it prints the `autoMemoryDirectory` that `.claude/settings.local.json`
(git-ignored; the value is a machine path) would get (dry run), and with `--apply` adds it when
neither that file nor `.claude/settings.json` has the key; exit 1 when the local file
cannot be edited (`kitlib/stubs/memory.py`; tests: `tests/stubs/test_memory.py`; docs/pm-start.md
"Memory folder (`memory`)").

Opt-in, for a repository that wants committed stubs (the old setup; decision 21 made the default
none). Nothing in the kit needs them: the skills, roles and the default profile run
`<kit>/tools/<name>.py -C <worktree>`, which works the same in a repository with stubs.

Logic: `kitlib/stubs/__init__.py`; tests: `tests/stubs/test_kit_stubs.py`,
`tests/stubs/test_live_root.py`.

A stub is `<checkout>/<tools_dir>/<name>.py`. It runs `<kit>/tools/<name>.py` in a child Python
with the stub's arguments.

- The kit's location is written into the stub. No environment variable names the kit, the
  repository or its config.
- That location is the live kit checkout (`kitlib.stubs.kit_root`). When `kit_stubs.py` runs
  from a pinned export of pushed main (a gate's `kit-stubs` step under a pinned gate.py or
  land.py, docs/gate.md "Pinned tools"), it is `KIT_LIVE_ROOT` (`kitlib.pluginsource.live_root`), never the
  export: `write` names the main checkout and `check` holds stubs that name it current. That holds
  for a gate started from the plugin's export too (`<kit>/tools/gate.py -C <checkout>`): it pins
  through the main checkout the export's marker names (`kitlib.pluginsource.export_checkout`,
  docs/plugin-source.md "Layout"; `tests/gate/test_export_stubs.py`).
- Run from an export with no `KIT_LIVE_ROOT` (`<kit>/tools/kit_stubs.py` itself, or the session
  start's stubs check, which runs in the plugin's export), the location is the main checkout the
  export's marker names, when that folder holds `kit.json` and `tools/` (`kitlib.stubs.live_kit`).
  An export whose marker names none is refused: `write`, and `check` on a repository that uses
  stubs, exit 2 with `kit_stubs: refused: <export> is an export of the kit ...` and write nothing,
  so no stub ever names an export a prune may remove (`tests/stubs/test_live_root.py`). A
  repository that uses no stubs is never refused (`check` below).
- The child runs in the caller's directory when that is inside the stub's checkout, else in the
  stub's checkout. So a kit tool's default `-C .` names the stub's checkout, and a linked
  worktree's stub acts on that worktree.
- When it moves the caller, the stub passes `[stub checkout, caller directory]` to the child in
  `KIT_STUB_CALLER` (kitlib.stubs.CALLER_VAR). It reads no variable itself. The gate uses it to
  refuse a caller in another checkout (docs/gate.md "The caller's checkout").
- The child's exit code is the stub's. A missing kit tool exits 2 with
  `refused: no kit tool at ...`.

Which tools get a stub: `--tools`, else `.kit.json` "stubs".tools, else every name in
`kitlib.stubs.SHARED` that the kit has as a command (a `tools/<name>.py` with a `__main__` block,
`kit_stubs` excepted). A name the kit has no command for is refused (exit 2), but for a tool the
kit removed (`kitlib.stubs.RETIRED`) named in "stubs".tools: that name is dropped, and `write` and
`check` print `retired  <path> (delete it)` for a stub of it still in the repository. The line is a
note: no exit code changes and nothing deletes the file.

- `write` creates or replaces each stub. It keeps a file that is not a stub, and reports it,
  unless `--replace`. `--dry-run` writes nothing.
- `check` exits 0 when every stub has exactly the text `write` would give it. A stub that runs
  another kit location is `stale`. A repository that uses no stubs (no `--tools`, no
  `"stubs".tools` in its `.kit.json` and no stub in its tools folder, such as a PM data folder)
  exits 0 with `kit_stubs: uses no stubs (...); nothing to check`, the rule the session start's
  check follows (`kitlib.stubs` `_opted`).
- Exit codes: 0 done, current, or no stubs used; 1 a problem named; 2 usage, configuration, or an
  export whose marker names no main checkout.

A committed stub carries the kit's absolute path, so a repository whose gate checks for machine
paths must allow it (`machine_paths.allow`, docs/kit-json.md).

## Not ported from the older PM kit

- kit_stubs: the `_kit_stub.py` runtime (`KIT_REPO_ROOT`/`KIT_CONFIG` env, runpy in-process);
  the `PM_KIT`/`DEV_ROOT` kit lookup; library stubs and the import-as-library mode (the shared
  libraries are `kitlib` modules); the warnings those modes needed. The old tool had no tests.
- new-repo.ps1: copying `kit\tools\**`, `templates\common` and `templates\stacks\<Stack>`
  wholesale, with placeholder substitution (a repository holds no copy of the kit; `-Stack` installs
  only the stack's `tools/` files (`gate.json` and the runners above), its skills and its ignore lines); writing the identities
  into `tools\doc_budget.json` (they go to `.kit.json` "owner").
