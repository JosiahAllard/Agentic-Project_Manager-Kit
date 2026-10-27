# Installing the kit

Three steps: point the machine at its PM repository, set the machine up once, then work in any repository, with nothing of the kit written into it (plan decision 27, docs/multi-host-plan.md section 7). Three repositories land through `gate.py land` on the owner's machine: this kit and two others. The zero repo setup cycle below is proven by a test (`tests/zerosetup/test_cycle.py`).

A repository never holds a copy of the kit or stubs of its tools, and by default nothing else
either: no `.kit.json`, no ignore lines. Skills, roles and the default profile run every kit tool from the
kit's folder, as `python <kit>/tools/<name>.py -C <worktree>` (decision 21 in
docs/multi-host-plan.md section 7; the kit header names `<kit>`, docs/content.md "Kit header").
A repository that wants its kit settings committed uses the committed setup ("Committed setup
(--repo)").

## Steps

0. **A Dev Drive** (optional, recommended; Windows only). Many agents and test suites make many
   small file writes (worktrees, test temp files, gate logs, git objects), and Defender's
   real-time scan of each can eat a large share of the CPU. A Dev Drive is a ReFS volume where
   Defender runs in performance mode, deferring each scan until after the file open. Put the kit,
   the repositories and their worktrees on it; the kit's own temp files (test runs, gate logs)
   then follow the checkout onto that drive (`kitlib/config/user.py` `work_temp`). Per Microsoft's
   Dev Drive guide (learn.microsoft.com/en-us/windows/dev-drive/):
   - Needs Windows 11 build 10.0.22621.2338 or later, local administrator rights, and at least
     50 GB. The C: drive cannot be one, and an existing volume cannot be converted.
   - Create it in Settings > System > Storage > Advanced storage settings > Disks & volumes >
     Create dev drive. `fsutil devdrv query <drive>:` shows whether it is trusted (performance
     mode needs that and real-time protection on); `fsutil devdrv trust <drive>:`, run as
     administrator, trusts it.

**Windows: apply the token-leak fix** (optional, recommended). It stops process launches slowing
down the longer the machine stays up: docs/windows-token-leak.md.

1. **The PM repository and its pointer, first, once per machine.** The machine config lives in
   the PM repository, never in the home folder: `<PM repo>/.pm-kit/config.json` (the policy keys)
   and `<PM repo>/.pm-kit/local.json` beside it (the machine-local and personal keys, the owner's
   email among them; `kitlib/config/user.py` `LOCAL_FILE_NAME`). Keep `local.json`, the hub's
   token folder `hub/` and `auto-tasks/` out of commits: the pre-push hook refuses a push that
   holds `local.json` or `hub/` (`kitlib/guard/prepush.py`), but nothing writes their ignore lines
   in a new PM repository, so add `.pm-kit/.gitignore` with the lines the config move writes
   (`kitlib/config/move.py` `GITIGNORE`: `local.json`, `hub/`, `auto-tasks/`,
   `managed-settings.json`, `*.bak*`, `*.move-tmp`). The kit finds that folder through one
   machine-wide registry value, the PM-repo pointer (docs/kit-json.md "The PM repo pointer"), so
   the pointer comes before anything that reads or writes the machine config.
   - **The PM repository** can be anywhere, on any drive: the main checkout of a clone of your PM
     repository, or a new one (`git init -b main <folder>`). The pointer names the top of its
     work tree, never a linked worktree; with PM data on a private branch
     (docs/pm-data-branch.md) it names that branch's work tree instead.
   - **Set the pointer**, from the kit's main checkout. Read the dry run, then run it again with
     `-Apply` from an elevated terminal:

     ```
     powershell -NoProfile -File installer\machine-setup.ps1 -PmRepo <PM repo>
     ```

     `-PmRepo` passes `--pm-repo` to `tools/machine_setup.py` and is a mode of its own: it takes
     only `-Python` and `-Apply` (exit 2 otherwise). It checks the folder as the reader does, sets
     `HKLM\SOFTWARE\pm-kit` value `pm_repo` and nothing else; `-Apply` is refused when the
     terminal is not elevated (docs/machine-setup.md "PM repo pointer").
   - **Until it is set**, every tool that needs the machine config refuses rather than fall back
     to the home folder: the intended state of a new machine, not a fault. `hub.py setup` and
     `hub.py serve` (token folder `<PM repo>/.pm-kit/hub`, `kitlib/hubsite/auth.py` `folder`) exit
     2 with one line, `machine config: no PM repo pointer is set, so the hub has no token folder:
     set it first ...`, and write nothing; machine setup (step 2) refuses to store the owner's
     email and does not ask for it.

2. **Machine setup, once per machine** (docs/machine-setup.md), after step 1, from the kit's main
   checkout, in a terminal:

   ```
   powershell -NoProfile -File installer\machine-setup.ps1 [-OwnerEmail <your commit email>] [-PmSettings <PM repo>\.claude\settings.local.json]
   ```

   Read the dry run, then run it again with `-Apply`. It adds the guard's entry to each Claude
   account's `settings.json`, installs the kit plugin per account from the export of the kit's
   pushed main, stores the owner's email in the PM repository's `.pm-kit/local.json` (`-OwnerEmail`;
   without it the `-Apply` run asks in the console, offering your `git config user.email`, which is
   never stored unconfirmed) and makes the agents' temp folder. It writes nothing into any
   repository but that machine config and the `-PmSettings` file, when you name one. Run it
   yourself: the guard refuses `-Apply` to agent sessions, since it stores the owner's email
   (docs/guard.md "Owner machine setup"). A machine set up with the old home-folder layout follows
   "Existing installs" below as well.

   The settings every account shares (the guard's hook, the status line, the plugin's marketplace)
   can instead live once in Claude Code's machine-wide managed settings file, under
   `%ProgramFiles%\ClaudeCode`. From an elevated terminal:

   ```
   powershell -NoProfile -File installer\machine-setup.ps1 -ManagedSettings [-Apply]
   ```

   It writes that file from the kit's template `hosts/claude/managed-settings.json`, merged with
   your own lines from `managed-settings.json` in the machine config folder when that file is
   there (docs/machine-setup.md "Managed settings"). A file already there is replaced whole, so
   the run is refused when it is not JSON or has keys that neither the template nor your overlay
   holds (they are named; add them to the template). With it in place the default mode adds no
   guard entry to any account, and notes when the file is out of date.
3. **A repository** needs nothing installed: any git checkout with an `origin` remote whose
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
4. **Overrides, when the defaults are not enough:** a `.kit.json` with only the keys that differ
   (docs/kit-json.md), committed under the owner's identity; a gate section that lists the
   repository's checks as `gate.steps` (a start per language:
   `templates/stacks/<stack>/tools/gate.json`); where landers edit the main checkout,
   `roles.lander.edit_main_checkout: true`. `<kit>/tools/gate.py` books the machine lock itself:
   do not wrap it in `machine_lock.py run`, or it books twice. The committed setup below writes a
   starting `.kit.json` and the rest in one step.
5. **Later:** turn on `repo.push_block` only after the repository has landed once through
   `<kit>/tools/land.py` or `gate.py land` (docs/go-live.md "Universal gate").

### Backlog records and the rulings ledger

`<kit>/tools/backlog.py -C <worktree> add` needs at least one `--scope <path>` (`none` for a record that touches no
tracked file); without one it refuses (docs/backlog.md). It also needs `--found-on <branch>` (or
`--found-on none` when found on no branch, or on one that landed and was removed) and `--why-not-folded "<reason>"` (docs/backlog.md
"The fold-in guard").

The rulings ledger is once per machine: set `ledger_dir` and `ledger_repos` (or `ledger_path` alone for one file) in
the machine config (docs/kit-json.md "Machine config keys"). The first `<kit>/tools/ledger.py add` creates the file from
`templates/common/LEDGER.md` (docs/ledger.md "A new ledger"). Every ledger write needs git to run, since it places the write lock (docs/ledger.md "Write lock").

### What first use writes

The first time the kit works in a repository (the PM's start, `tools/new_worktree.py`), the
first-use prep (`kitlib/git/prep.py`; docs/new-worktree.md "First-use prep") writes local git
settings only, never a tracked or untracked file, and nothing is committed:

- `.git/info/exclude` (in the git common folder, so every worktree shares it): one anchored line
  per agent worktree folder, `/.claude/worktrees/` and `/.codex/worktrees/` (each host's
  `worktrees_directory` in `hosts.json`, and the repository's own `worktrees_dir` when its
  `.kit.json` sets one). The worktrees then never show in the main checkout's `git status`.
- `core.hooksPath` (local) = `<kit>/githooks`, the kit's pre-push and pre-commit hooks, only when
  no hooks path is set at any level and the repository's own `.git/hooks` holds no hook. An
  existing value is kept.

Each step is idempotent. `tests/zerosetup/test_cycle.py` proves the whole cycle on a fresh clone
of an unrelated repository with nothing of the kit in it (its docstring lists what each platform
variant stands in for).

## Committed setup (--repo)

For a repository that wants its kit settings committed: `python <kit>/tools/machine_setup.py --repo <checkout> ...`
(`kitlib/machinesetup/repo.py`), or its PowerShell front end `installer/new-repo.ps1`, which
passes its parameters through (`-Target` is `--repo`, `-OwnerEmail` `--owner-email`, and so on).
Nothing in the kit needs it.

1. **The repository** must be a git checkout's top level, with an `origin` remote and a first
   commit (step 3 above); the setup warns, and prints the commands, when it finds no `origin`.
2. **Dry run**, from the kit checkout:

   ```
   python <kit>/tools/machine_setup.py --repo <checkout> --owner-email <owner> --agent-email <agents> [--main-branch main] [--repo-name <name>] [--stack python]
   powershell -NoProfile -File installer\new-repo.ps1 -Target <checkout> -OwnerEmail <owner> -AgentEmail <agents> [-MainBranch main] [-RepoName <name>] [-Stack python]
   ```

   It prints what it would write: `.kit.json` (`"repo".main_branch`, `"repo".name`,
   `"backlog".store` `"ref"`, `"owner".email`, `"owner".agent_emails`), `.gitignore` and
   `.gitattributes`, a starter `docs/INDEX.md`, with
   `--stack` the stack's `tools/gate.json`, and under `memory:` the `autoMemoryDirectory` key
   (`<the main checkout's drive>/Agent-memory/<repo folder name>`, `kitlib/stubs/memory.py`): added
   to `.claude/settings.local.json` only when neither that file nor `.claude/settings.json` has it,
   kept when either names any absolute folder, refused when the local file is not JSON it can edit or
   the key is not an absolute folder. The key is a machine path, so it goes in the git-ignored local
   file and there is nothing to commit. It never makes the folder.
   `"repo".name` is the repository's rulings-ledger column (docs/backlog.md "Owner gates"): `--repo-name`,
   else the target folder's name lower-cased; either must match `[a-z0-9][a-z0-9-]*` or it
   refuses (exit 2). Both emails are required and must differ, or an agent's commit could pass
   as the owner's.
   - `.gitignore` and `.gitattributes` start from `templates/common` (`.claude/worktrees/` and
     `.codex/worktrees/`) plus the stack's `gitignore` and `gitattributes` lists. A file already
     there is never overwritten: only the lines it lacks are appended, under `# Added by the kit
     installer`, and the dry run lists them.
   - `docs/INDEX.md` is written only when absent, and only by this setup: it opts the repository
     into the doc index check (decision 26). With an index file every tracked `.md` must be named
     on an entry line of it; the starter names only itself, so the dry run and `--apply` list
     the target's tracked `.md` files it does not name (advice only, never a refusal). With no
     index file the check (run by preflight, commit and review_pack) is skipped and says so.
   - `--stack dotnet|godot|java|python` copies `templates/stacks/<stack>/tools/gate.json` to
     `tools/gate.json` (refused if one is there) and adds `"gate": "tools/gate.json"` to
     `.kit.json`, and copies the stack's other `tools/` files (dotnet: `tools/dotnet_each.py`; godot:
     `tools/godot.cmd`; python: `tools/js_tests.py`; java has none), keeping any that is there already. It also installs the stack's
     `.claude/skills/**` (`kitlib.stackskill`, as `tools/stack_skill.py install`; only Godot has one, `godot-render-check`): each `.md` has `{kit_root}`
     replaced by the kit's absolute path with forward slashes, a file already there is kept, and a
     kit root it cannot write (no `tools/machine_lock.py` under it, or a double quote, brace, `$`
     or backtick in the path) refuses the whole run before anything is written, as does a stack
     the kit has no folder for, and, for the PowerShell front end, no python on PATH. Edit the
     gate's globs and test command to the project. The python gate runs
     `unittest discover -s tests` and needs one test: its pass check wants a `Ran N tests` line
     with N of 1 or more. It runs without `-t .`, so test modules import as top-level names: a
     `tests/__init__.py` layout with relative imports breaks (use absolute imports, or change the
     command). Without `--stack` there is no gate section and `gate.py` detects the stack (step 3
     above), or write `gate.steps` yourself (docs/kit-json.md).
3. **Apply:** run it again with `--apply` (`-Apply`), then commit what it wrote. `.kit.json` and
   `tools/gate.json` are owner-only paths: commit them under the owner's identity, the rest
   (`.gitignore`, `.gitattributes`, `docs/INDEX.md`) as usual. It
   refuses before writing anything when `.kit.json` or `tools/gate.json` exists. A write that fails (a read-only file, a full disk) stops it with
   `new-repo: FAILED`, the files already written, and exit 1. Exit codes: 0 done or dry run;
   1 refused or a write failed; 2 usage.
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
     check); 130 Ctrl+C. On a non-zero code the target's files stay written and it prints the
     command to run again on its own, quoted to paste into PowerShell. Ctrl+C may end the setup
     before its summary: then run `pm_data.py init --adopt` with the same `--path` to check what
     was made. The PowerShell front end takes a relative `-Target` or `-PmData` from the
     PowerShell location, and refuses a `"` in either (usage, 2). A repository already set up
     (its `.kit.json` makes the setup refuse) runs that command directly.
4. **An existing repository with its own copies of the tools** skips the setup. It needs a
   `.kit.json` only when it wants more than the defaults (docs/kit-json.md), and one that
   writes backlog records names its store there (`"backlog": {"store": "ref"}` or a folder:
   the default store alone is read-only, docs/backlog.md "Stores"); its
   own copies are then removed, since agents run the kit's tools from `<kit>`.
5. **Git hooks:** the first-use prep sets them ("What first use writes"); by hand,
   `git -C <repo> config core.hooksPath <kit>/githooks`. This installs `pre-push` and
   `pre-commit`; `pre-commit` is inert unless the repository opts in (docs/ledger.md "Pre-commit check").

### What the committed setup does not install

- The stack's `.github/workflows/{quick,full}.yml` (owner-only paths; copy them by hand).
- `templates/common/AGENTS.md` (with `templates/common/CLAUDE.md`, the one line `@AGENTS.md`) and the two profile templates (`agent-profile.TEMPLATE.md`,
  `coordinator-profile.TEMPLATE.md`): the repository's rules file and profiles are drafted from
  them with the `<PLACEHOLDER>` slots filled (docs/shipping.md "Stack templates"); their default
  rule lines (the owner's go list, the creative limits) stay unless the owner deletes one. Both profiles
  are optional: with no agent profile, every role reads the kit's `content/default-profile.md`
  plus the repository's README and instructions file (docs/agent-profile.md).
- `.claude/settings.json`: the kit ships none (docs/go-live.md "Joint-review questions"); the
  setup adds only its `autoMemoryDirectory` key, to the ignored `.claude/settings.local.json`.
- Anything outside the repository: the ledger, the lock and the user config are per machine.

### Plugin install (local marketplace)

Each account folder installs the plugin from the kit's export of pushed main, not from the
checkout (docs/plugin-source.md). `python tools/plugin_refresh.py --migrate --apply` adds the
export root as the marketplace `pm-kit` and installs `pm-kit@pm-kit` in every account folder; after
each kit landing `python tools/plugin_refresh.py --apply` refreshes them. Each is a dry run without
`--apply`. Migrate one folder first (`--folder DIR`, repeatable), check a session there, then the
rest; `--apply` refuses a folder that is not migrated (docs/plugin-source.md "Migrating"). The
`claude plugin` subcommands these run are inferences except `plugin update`, and Claude Code
copies the plugin into its cache (observed; docs/plugin-source.md "Open questions"). Either way `${CLAUDE_PLUGIN_ROOT}` holds pushed kit main, not the live checkout:
the skills and agents write every kit path as `<kit>/<path>` (tests/skills and
tests/content/test_skill_commands.py check it); the kit header hook tells each session and
subagent the folder `<kit>` stands for (docs/content.md "Kit header"). `${CLAUDE_PLUGIN_ROOT}` is
substituted in a plugin skill's text (owner's machine test, Claude Code 2.1.302); agent text was
not tested, so no kit agent or skill uses it. The Desktop app (Code tab) picks the plugin up and
lists its skills under `/` as `pm-kit:<skill>`.

The plugin root is the whole kit (export), and `.claude-plugin/plugin.json` stays at its root: the
hooks and the MCP servers run kitlib and tools beside them. plugin.json, marketplace.json and the
hooks file `hosts/claude/shell/hooks/hooks.json` (named by `"hooks"` in plugin.json) are rendered
from `kit.json` and content/ (docs/content.md "Manifests"). The plugin's hooks file carries three
entries, each run as `pythonw` with the kit's one hook entry,
`${CLAUDE_PLUGIN_ROOT}/boot/hook.py claude <event>`: the kit header on SessionStart (matcher
`startup|resume|clear|compact`) and on SubagentStart, and the coordinator SessionStart hook
(`boot/hook.py claude SessionStart coordinator`, matcher `startup|resume|compact`). The guard
(`boot/hook.py claude PreToolUse`) stays in each account's settings (or the managed settings file)
and is never in the plugin (docs/guard.md "Go-live"); the PM checklist (`boot/hook.py claude
SessionStart pm`) stays in the PM repo's settings. An account whose settings still run the
coordinator hook runs it twice with the plugin: remove the settings entry (`tools/machine_setup.py`
notes it, docs/machine-setup.md "Settings").

Machine setup (`machine-setup.ps1 -Apply`, its default mode) does the same migrate for each account folder
whose plugin is not enabled from the export: it makes the export, then runs the migrate steps for
that folder through the same code (docs/machine-setup.md "Default mode"). The CLI also
rewrites other keys of that `settings.json`, so the step backs the file up first.

### After the plugin install: remove the per-item junctions

Junctions an earlier setup made under `~/.claude` (`skills/*`, `agents`, `hooks`, `tools/*`,
`knowledge/*`) load what the plugin now loads. Machine setup makes no link: it reports each
leftover and re-points a `settings.json` path through `~/.claude/tools/<name>` to the kit's own
file (docs/machine-setup.md "Links"). Run it with `--apply` first, then remove each link with the
line its dry run prints, which removes the link only, never the target (never `/s`):

```
leftover link <path> -> <target>: remove it with: cmd /c rmdir "<path>"
```

A second dry run then reports no leftover link. The kit's `skills/*` links go at the plugin
install, or each skill appears twice. Only the current kit skill names and the retired
`subagent-dispatch`, `doc-claims-discipline`, `specialty-records` and `specialty-spec` are
touched; any other link stays:

```powershell
$kit = 'backlog-ops','backlog-pricing','branch-review','branch-sync','builder','coordinator-dispatch','flaky-test-triage','hub-ops','ledger-ops','land-branch','machine-lock-run','mutation-testing','session-handoff','shell-and-worktree-traps','subagent-dispatch','doc-claims-discipline','specialty-records','specialty-spec','specialty-3d-assets','specialty-rigging','specialty-tech-art','specialty-sound','specialty-review','specialty-research','specialty-docs'
Get-ChildItem "$env:USERPROFILE\.claude\skills" -Force |
  Where-Object { $_.LinkType -eq 'Junction' -and $_.Name -in $kit } |
  ForEach-Object { cmd /c rmdir "$($_.FullName)" }
```

Gated on the plugin shipping the kit's agents under the pm-kit: names; do not do this before.

Remove ~/.claude/agents only after the plugin providing pm-kit agents is installed and the launcher uses pm-kit:coordinator.

Remove the `hooks` link only when `settings.json` no longer calls a guard through `~/.claude/hooks`.

## Existing installs: a machine with the old home-folder layout

A new machine skips this section. A machine an older machine setup ran on can hold leftovers of
the old layout; each is cleared once, after step 1:

- **An older kit clone:** run machine setup with `-OldKit <that clone>` instead of the default
  mode (docs/machine-setup.md).
- **A machine config in the home folder** (`~/.pm-kit`): with the pointer set (step 1), move it
  into the PM repository with `tools/config_move.py`, a dry run first, then `--apply`
  (docs/kit-json.md "Moving the machine config into the PM repo"). Until it is moved, the kit
  reads neither copy: a leftover home config is refused with the move's command
  (`kitlib/config/user.py` `UNMOVED`), the Action Hub's tools included (`kitlib/hubsite/auth.py`
  `folder`).
- **Links under `~/.claude`** (junctions an earlier setup made): remove them as in "After the
  plugin install" above.

## Agent memory folder: tools/kit_stubs.py memory

```
python <kit>/tools/kit_stubs.py memory [-C DIR] [--apply]
```

It prints the `autoMemoryDirectory` that `.claude/settings.local.json` (git-ignored; the value is
a machine path) would get (dry run), and with `--apply` adds it when neither that file nor
`.claude/settings.json` has the key. Exit 1 when either file cannot be read as a JSON object, 2 on
usage or a host registry it cannot read. The command it prints names the kit's main checkout, also
when it runs from the plugin's export (`kitlib.stubs.kit_root`: the checkout the export's marker
names, docs/plugin-source.md "Layout"). Logic: `kitlib/stubs/memory.py`; docs/pm-start.md "Memory
folder (`memory`)".
