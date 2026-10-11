# Installing the kit in a repository

Held for the joint review: no repository runs the kit yet. The installer and the steps below were run end to end on a scratch repository (a local bare origin, `-Stack python`): a green `gate.py`, a worktree that left main clean, and a `land.py` landing.

A repository never holds a copy of the kit: only `.kit.json` (its settings) and stubs, small
files in its tools folder that run the kit's tools.

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

1. **A new repository** must be a git checkout with an `origin` remote and a first commit:
   `git init -b main`, `git remote add origin <url>`, a commit, `git push -u origin main`.
   `tools/new_worktree.py` fetches `origin` (it refuses if the fetch fails), and `tools/preflight.py` does only with `--fetch`
   (it never refuses for a missing remote); the installer warns, and prints the commands, when
   it finds none.
2. **Dry run** `installer/new-repo.ps1` from the kit checkout:

   ```
   powershell -NoProfile -File installer\new-repo.ps1 -Target <checkout> -OwnerEmail <owner> -AgentEmail <agents> [-MainBranch main] [-Stack python]
   ```

   It prints what it would write: `.kit.json` (`"repo".main_branch`, `"owner".email`,
   `"owner".agent_emails`), the stubs (by `kit_stubs.py write -C <target>`), `.gitignore` and
   `.gitattributes`, a starter `docs/INDEX.md`, and with `-Stack` the stack's `tools/gate.json`.
   The target must be a checkout's top level. Both emails are required and must differ, or an
   agent's commit could pass as the owner's.
   - `.gitignore` and `.gitattributes` start from `templates/common` (`.claude/worktrees/` and `.codex/worktrees/`
     are the lines that matter: without them the first agent worktree dirties the main checkout and a
     landing refuses "main checkout is not clean") plus the stack's `gitignore` and
     `gitattributes` lists. A file already there is never overwritten: only the lines it lacks
     are appended, and the dry run lists them.
   - `docs/INDEX.md` is written only when absent. The doc index check (run by preflight, commit
     and review_pack) needs the file, and every tracked `.md` named on an entry line of it. The starter names only itself: when it is
     written, the dry run and `-Apply` list the target's tracked `.md` files it does not name
     (under "add these to docs/INDEX.md"; advice only, never a refusal), for you to add.
   - `-Stack dotnet|godot|java|python` copies `templates/stacks/<stack>/tools/gate.json` to
     `tools/gate.json` (refused if one is there) and adds `"gate": "tools/gate.json"` to
     `.kit.json`, and copies the stack's other `tools/` files (only Godot has one, `tools/godot.cmd`),
     keeping any that is there already. Edit the gate's globs and test command to the project.
     The python gate runs
     `unittest discover -s tests` and needs one test: its pass check wants a `Ran N tests` line
     with N of 1 or more, so a run of none fails on every Python version. It runs without
     `-t .`, so test modules import as top-level names: a `tests/__init__.py` layout with
     relative imports breaks (use absolute imports, or change the command).
     Without `-Stack` there is no gate section and `gate.py` refuses ("no gate step selected"):
     the closing text prints the copy-and-point step, or write `gate.steps` yourself
     (docs/kit-json.md).
3. **Apply:** run it again with `-Apply`, then commit what it wrote. `.kit.json` and
   `tools/gate.json` are owner-only paths: commit them under the owner's identity, the rest
   (stubs, `.gitignore`, `.gitattributes`, `docs/INDEX.md`) as usual. It refuses before writing
   anything when `.kit.json` or `tools/gate.json` exists, or when a file that is not a stub sits
   where a stub goes. A write that fails (a read-only file, a full disk) stops it with
   `new-repo: FAILED`, the files already written, and exit 1. Exit codes: 0 done or dry run;
   1 refused or a write failed; 2 usage. Tests:
   `tests/stubs/test_new_repo.py`, against a throwaway repository only.
   - **PM data on a private branch** (optional; docs/pm-data-branch.md): add
     `-PmData <folder> [-PrivateRemote <url>]`. After the files above are written, the script
     runs `python <kit>/tools/pm_data.py -C <kit> init --path <folder> [--url <url>]`: the data
     branch, its locked worktree at `<folder>` and the private remote belong to the kit's own
     repository, not the target's, and `<kit>` must be the kit's main checkout (`init` refuses a
     linked worktree). The dry run prints that command and runs nothing. `-PrivateRemote`
     without `-PmData` is a usage error (2). The script prints `pm_data.py`'s lines and exits
     with its code: 0 done; 1 refused (nothing changed in the kit); 2 a configuration it cannot
     read; 3 made locally, the push failed (its lines say how to recover); 4 stopped part-way
     (rerun with `--adopt`); 5 crashed (what it made is not known; rerun with `--adopt` to
     check); 130 Ctrl+C. A Python traceback (exit 1) is also reported as a crash, not as a
     refusal. On a non-zero code the target's files stay written and the script prints the
     command to run again on its own, quoted to paste into PowerShell. Its lines are shown as
     they come, but Ctrl+C may end the installer before its summary: then run
     `pm_data.py init --adopt` with the same `--path` to check what was made. A relative
     `-PmData` is taken from the PowerShell location; a trailing `\` on `-PmData` or
     `-PrivateRemote` is dropped (a drive root is kept), and a `"` in either is a usage error. A repository already installed (its `.kit.json` makes the
     script refuse) runs that command directly.
4. **An existing repository with its own copies of the tools** skips the installer. It needs a
   `.kit.json` with the same keys (docs/kit-json.md), then
   `python <kit>/tools/kit_stubs.py write -C <repo> --replace`, then a commit.
5. **Check the stubs:** `python <kit>/tools/kit_stubs.py check -C <repo>` exits 0.
6. **Git hooks:** `git -C <repo> config core.hooksPath <kit>/githooks`. This installs `pre-push` and `pre-commit`; `pre-commit` is inert unless the repository opts in (docs/ledger.md "Pre-commit check").
7. **Gate:** with `-Stack` the gate section exists; otherwise list the repository's checks as
   `gate.steps` in `.kit.json` (docs/kit-json.md; a start per language:
   `templates/stacks/<stack>/tools/gate.json`). Where landers edit the main checkout, also set
   `roles.lander.edit_main_checkout: true`. `tools/gate.py` books the machine lock itself: do not
   wrap it in `machine_lock.py run`: the gate would then run as a nested lock run, booking twice.
8. **Later:** turn on `repo.push_block` only after the repository has landed once through
   `tools/land.py` or `gate.py land` (docs/go-live.md "Universal gate").

### What the script does not install

- The stack's other files: `.github/workflows/{quick,full}.yml` (owner-only paths; copy them by
  hand), and for Godot the render-check skill (`.claude/skills/godot-render-check/SKILL.md`).
  `tools/godot.cmd` is installed.
- `templates/common/CLAUDE.md` and the two profile templates (`agent-profile.TEMPLATE.md`,
  `coordinator-profile.TEMPLATE.md`): the repository's rules file and profiles are drafted from
  them with the `<PLACEHOLDER>` slots filled (docs/shipping.md "Stack templates").
- `.claude/settings.json`: the kit ships none (docs/go-live.md "Joint-review questions").
- `backlog/.gitkeep` is not needed (the backlog store is the ref `refs/backlog/main`), and
  `.github/workflows/instructions.yml` is not shipped: the stack workflows do not mention it.
- Anything outside the repository: the ledger, the lock and the user config are per machine.

### Tools without a stub

The stubs cover the tools a repository's agents run from their own checkout (`kitlib.stubs.SHARED`),
including `land.py` and `ledger.py`. The tools in the list below run by the kit's path,
`python <kit>/tools/<name>.py`, and get no stub:

- `machine_lock.py`: one lock per machine, so it runs from the machine's lock install, never a
  repository's. `run` needs `--repo <name> --slug <slug>` (and `--kind`); `status` needs nothing.
  A repository's own commands use it only through a gate (booked by `gate.py`) or a wrapped heavy
  run.
- `kit_tests.py` (the kit's own suites), `redfirst.py`, `lock_release_check.py`,
  `daily_full.py`, `backlog_lint.py`, `agent_roster.py`: run by the kit's maintainers or the PM
  against a repository named by `-C` (docs/test-tools.md, docs/redfirst.md, docs/lock-release.md,
  docs/daily-full.md, docs/backlog-lint.md, docs/ROSTER.md).

### Plugin install (local marketplace)

`.claude-plugin/marketplace.json` lists the plugin with `"source": "./"`, so a kit checkout is a
marketplace of its own. Add it by path and install from it: `claude plugin marketplace add <kit>`,
then `claude plugin install <plugin name in .claude-plugin/plugin.json>@pm-kit`. A relative-path plugin of a local marketplace
loads in place, so `${CLAUDE_PLUGIN_ROOT}` is the checkout the stubs run: the skills and agents write
every kit-only path (the tools above, `kitlib/`, `optional/`, the kit's docs) as
`${CLAUDE_PLUGIN_ROOT}/<path>` (tests/skills checks it), and only stub tools repo-relative. Source:
https://code.claude.com/docs/en/plugin-marketplaces. That `${CLAUDE_PLUGIN_ROOT}` is substituted in
agent and skill text is unverified: confirm it in one real loaded session before the first install.
`claude plugin validate` still reports the
plugin name as reserved (a `claude-` prefix); the name is unchanged and installs.

`tools/backlog.py add` needs at least one `--scope <path>` (`none` for a record that touches no
tracked file); without one it refuses (docs/backlog.md).

The rulings ledger is once per machine: set `ledger_path` (and optionally `ledger_repos`) in
the machine config (docs/kit-json.md "Machine config keys"). The first `tools/ledger.py add` creates the file from
`templates/common/LEDGER.md` (docs/ledger.md "A new ledger").

## Stubs: tools/kit_stubs.py

```
python tools/kit_stubs.py write [-C DIR] [--tools a,b,...] [--replace] [--dry-run]
python tools/kit_stubs.py check [-C DIR] [--tools a,b,...]
```

Logic: `kitlib/stubs/__init__.py`; tests: `tests/stubs/test_kit_stubs.py`.

A stub is `<checkout>/<tools_dir>/<name>.py`. It runs `<kit>/tools/<name>.py` in a child Python
with the stub's arguments.

- The kit's location is written into the stub. No environment variable names the kit, the
  repository or its config.
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
`kit_stubs` excepted). A name the kit has no command for is refused (exit 2).

- `write` creates or replaces each stub. It keeps a file that is not a stub, and reports it,
  unless `--replace`. `--dry-run` writes nothing.
- `check` exits 0 when every stub has exactly the text `write` would give it. A stub that runs
  another kit location is `stale`.
- Exit codes: 0 done or current; 1 a problem named; 2 usage or configuration.

A committed stub carries the kit's absolute path, so a repository whose gate checks for machine
paths must allow it (`machine_paths.allow`, docs/kit-json.md).

## Not ported from the older PM kit

- kit_stubs: the `_kit_stub.py` runtime (`KIT_REPO_ROOT`/`KIT_CONFIG` env, runpy in-process);
  the `PM_KIT`/`DEV_ROOT` kit lookup; library stubs and the import-as-library mode (the shared
  libraries are `kitlib` modules); the warnings those modes needed. The old tool had no tests.
- new-repo.ps1: copying `kit\tools\**`, `templates\common` and `templates\stacks\<Stack>`
  wholesale, with placeholder substitution (a repository holds stubs only; `-Stack` installs
  only the stack's `tools/` files (`gate.json`, and Godot's `godot.cmd`) and its ignore lines); writing the identities
  into `tools\doc_budget.json` (they go to `.kit.json` "owner").
