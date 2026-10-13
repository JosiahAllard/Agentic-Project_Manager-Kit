# Machine setup

One reviewed command switches a machine's Claude config folders from an older kit clone to this kit checkout: the links, each account's `settings.json`, and the machine extras (the schedule shortcut, the agents' temp folder with its sweep, and `KIT_MACHINE_LOCK`). It is the switch-over command for the joint review (docs/go-live.md "Switches"). With `--fresh` it sets up a machine that has no older kit clone instead ("Fresh machine (--fresh)" below). Nothing has run it on a real machine yet.

```
powershell -NoProfile -File installer\machine-setup.ps1 -OldKit <old kit clone> [-ConfigDir <folder>,<folder>] [-AgentTemp <folder>] [-RegisterSweepTask] [-Apply]
powershell -NoProfile -File installer\machine-setup.ps1 -Fresh [-PmSettings <file>] [-ConfigDir <folder>,<folder>] [-AgentTemp <folder>] [-RegisterSweepTask] [-Apply]
python tools/machine_setup.py (--old-kit DIR | --fresh [--pm-settings FILE]) [--config-dir DIR]... [--agent-temp DIR] [--kit DIR] [--home DIR] [--desktop DIR] [--register-sweep-task] [--apply]
```

Run it from the kit's main checkout, not a worktree: `--kit` defaults to the checkout the tool is in, and every link, hook path and shortcut names that folder.

- **Dry run by default.** It prints every change, with each `settings.json` change as a unified diff, and changes nothing. `--apply` (`-Apply`) makes the same changes, in the printed order.
- **Config folders.** Defaults to `~/.claude` and every `~/.claude-*` folder except the machine config's own, current and retired (`kitlib.config.user.DIR_NAME`, `kitlib.config.migration.OLD_DIR_NAME`). `--config-dir` (repeatable) names them instead; the wrapper's `-ConfigDir a,b` is split on commas, so a folder name with a comma cannot be passed there. A folder that is a link to one already listed is read once. `--fresh` defaults to the account folders `tools/plugin_refresh.py` runs in (`account_folders`, `kitlib/pluginsource/claude.py`): the same folders plus each existing folder the launcher config names, and the dry run notes that a backup copy such as `~/.claude-backup` is among them unless `--config-dir` leaves it out. With no config folder at all, both modes refuse.
- **Idempotent.** A second run prints `nothing to do` in every section and exits 0.
- **Refusals.** Any refusal stops both modes before anything changes: an old-kit path in a settings file that this step neither removes nor re-points, a hook path it cannot resolve, a settings file it cannot read or must not replace, a kit file a link or path would need, a file where a link goes, a missing config folder, or `--kit` equal to `--old-kit`. Each refusal names the file and path.
- **Part-way failure.** If a change fails under `--apply`, whatever the error, it stops there. It prints the change, the error, how to restore that change by hand, and the changes already made. Every line is printed as it is produced.

Exit codes: 0 done, dry run printed or nothing to do; 1 refused or stopped; 2 usage: neither or both of `--old-kit` and `--fresh`, or `--pm-settings` without `--fresh` (the wrapper also exits 2 for the same with `-OldKit`, `-Fresh` and `-PmSettings`, for a `-Python` that is not a file, or, without `-Python`, with no `python` on PATH).

The wrapper runs the tool under `-Python <python.exe>` when given, else under the `python` on PATH. The tests pass their own interpreter (`sys.executable`): with the Python install manager, the `python` alias reads its state under `%LOCALAPPDATA%`, and a run with a temp `LOCALAPPDATA` made it install a new Python there.

## Links

`kitlib/machinesetup/links.py`. In each config folder it checks `tools`, `knowledge` and `skills`:

- `tools`: holds a junction per entry. `machine_lock` -> `<kit>/tools`, `kitlib` -> `<kit>/kitlib`, and `comfy`, `procwatch`, `usage`, `agenttemp` -> `<kit>/optional/<name>`.
  - It cannot be one junction onto a kit folder. The live lock path is `tools/machine_lock/machine_lock.py`, and the kit's `tools/machine_lock.py` imports `kitlib` from beside the folder it was started from, spelled through the junction (`os.path.abspath` does not resolve it).
- `knowledge/asset-generation` -> `<kit>/knowledge/asset-generation`.
- `skills/<name>` -> `<kit>/hosts/claude/shell/skills/<name>`, for each old-kit skill the kit has. One it lacks (a repo-local skill) is noted and keeps pointing at the old kit, linked again when its container is replaced.

A container that is itself a junction into the old kit is replaced by a real folder of these links. The old-kit entries it no longer reaches are noted, not removed.

A container that is a link elsewhere, for example an account folder following `~/.claude`, is left alone. So is an absent container.

Each entry is handled the same way:
- A link to the wrong place is replaced. A link is removed with `rmdir`, which removes the link only.
- A real folder is renamed aside to `<name>.old-<yyyymmdd>` and then linked. Nothing is deleted.

## Settings

`kitlib/machinesetup/settings.py`. In each config folder's `settings.json`, it reads every path in a hook's `command` and `args` and in `statusLine.command`. `~`, `%USERPROFILE%`, `$env:USERPROFILE`, `$HOME`, `${HOME}`, `$env:HOME`, `%HOMEDRIVE%%HOMEPATH%` and `%HOMEPATH%` are read as the home folder.

A path it cannot resolve to an absolute one (a relative path, a script name, another variable) is refused, since it cannot tell whether it is the old kit's. Words, switches such as `/c`, URLs and paths under `$CLAUDE_PROJECT_DIR` or `$CLAUDE_PLUGIN_ROOT` are not refused. A `hooks` value, event or group of a shape Claude Code does not use is refused too.

A path that resolves into the old kit, into `<kit>/hooks` or to `<kit>/tools/guard.py` (the pre-move paths, go-live.md "Switches") gets one of three outcomes:
- **Removed.** `no_root_search.py` and `main_checkout_redirect_guard.py`: the hook goes. A group or event that removal empties goes too; one that was already empty stays.
- **Re-pointed.** The file is rewritten to the kit's copy with forward slashes, quotes kept:
  - `pm_session_start.py` and `coordinator_session_start.py` become the kit's hook entry: `boot/hook.py claude SessionStart pm` and `boot/hook.py claude SessionStart coordinator` (the words follow the path in a command line, or are added to `args` after it).
  - `statusline-usage.ps1` and `write-usage-status.ps1` go to `optional/usage/`.
- **Refused.** Any other old-kit path, named.

A path through `<config>/tools/<link>` stays as written: the links step points it at the kit. It is refused when the kit has no file there.

When no PreToolUse hook names `<kit>/boot/hook.py`, the entry `boot/hook.py claude --settings-entry` prints is appended (docs/guard.md). The host, `claude`, is hosts.json's default host, whose account settings these are.

A SessionStart hook in shell form whose command is a Python interpreter (`python`, `python.exe`, `pythonw`, `pythonw.exe`, by base name), then `<kit>/boot/hook.py` and its words (at most three plain words; a redirect or a pipe keeps the hook in shell form), is written in exec form: `command` becomes the interpreter of the guard entry `boot/hook.py claude --settings-entry` prints, and `args` holds the script with forward slashes, then its words. Exec form starts no shell. Any other command, a hook that already has `args`, and every other event are left as they are.

When the kit's `.claude-plugin/plugin.json` names a `hooks` file, a settings hook that runs the same script with the same words as one of that file's hooks, for the same event, is noted (`${CLAUDE_PLUGIN_ROOT}` read as the kit): once the plugin is enabled the hook runs twice, so the settings entry is removed in the same step as the plugin install (docs/install.md "Plugin install"). The note changes nothing.

`statusLine` stays in shell form. Claude Code gives it no `args` field: "The `command` field runs in a shell", and "On Windows, Claude Code runs status line commands through Git Bash when Git Bash is installed, or through PowerShell when Git Bash is absent" (https://code.claude.com/docs/en/statusline).

Each of these is a planned change or a note: a dry run writes nothing, and `--apply` writes through the backup below.

The file is edited as JSON and written back in its own layout: indent, ASCII escaping, CRLF, BOM and final newline. Only the changed lines differ. A layout `json.dumps` cannot reproduce is noted, and the diff then shows the whole rewrite.

A `settings.json` that is a symbolic link or has more than one hard link is refused when it needs a change: writing through a temp file would split it from the file it shares.

`--apply` refuses if the file changed since the survey or has become a link. It then copies the file to `settings.json.machine-setup-<yyyymmdd>.bak` and writes through a temp file, which it removes if the write or the rename fails.

### Moved hook paths

The hooks lived in `<kit>/claude/hooks/` until the hosts/ move (docs/multi-host-plan.md step 7), then
in `<kit>/hosts/claude/shell/hooks/` as one script each. Since step 14 every hook runs the kit's one
entry, `<kit>/boot/hook.py <host> <event> [<part>]`, and those scripts are gone; the skills are in
`<kit>/hosts/claude/shell/skills/`. Settings and links written before still name the old paths, so:

- **Settings.** Before any other step, every hook path (any event, `command` and `args`) to a retired
  script in `<kit>/claude/hooks/` or `<kit>/hosts/claude/shell/hooks/` is re-pointed to the entry
  with the host and the script's words (`settings.repoint_moved`, `settings.RETIRED`): `guard.py`
  becomes `boot/hook.py claude PreToolUse`, `pm_session_start.py` `boot/hook.py claude SessionStart
  pm`, `coordinator_session_start.py` `boot/hook.py claude SessionStart coordinator`. In a command
  line the words follow the path; in `args` they are added after it. This runs in every mode, fresh
  too, and in the `--pm-settings` file, so the guard entry is re-pointed instead of a second one
  being added, and the PM hook is re-pointed instead of refused. Any other script name in those
  folders, or a kit with no `boot/hook.py`, is refused.
- **Skill links.** A link in a real `skills` folder that points into `<kit>/claude/skills/` (dangling
  after the move) is re-pointed to the same skill in `<kit>/hosts/claude/shell/skills/`
  (`links.MOVED_SKILLS_DIR`; switch mode only, as the links step is).
- **No stubs.** Plan decision 28 removed the `<kit>/claude/hooks/` stubs with step 14. An account
  whose settings still name a retired script runs no guard script at all, and Claude Code then
  blocks every guarded tool call (its message is Python's `can't open file '<kit>/.../guard.py'`),
  so the owner re-runs machine setup once on each machine (dry run, then `--apply`, in its mode:
  `--old-kit DIR` or `--fresh --pm-settings FILE`; go-live.md "Switches"). It runs from a terminal,
  not from a Claude session, whose Bash calls are blocked until then, and only after the kit
  checkout the settings name has `boot/hook.py` (before that it refuses). A Claude session reads its
  hooks when it starts, so the sessions open at the time are restarted. A hook command line the entry
  itself cannot use (no host, a host or event it does not know) is blocked with a message naming
  `tools/machine_setup.py`. A PM repo's own settings file is re-pointed only with `--fresh
  --pm-settings FILE`; under `--old-kit`, one that still runs `pm_session_start.py` is edited by hand
  to the hook in docs/pm-start.md (a stale one drops the checklist and blocks nothing).

## Extras

`kitlib/machinesetup/extras.py`. Each extra is read first and planned only when it differs. `Host` is the Windows side; tests pass a fake.

- **Machine schedule shortcut.** `Machine schedule.lnk` on the desktop (or `--desktop`) runs `pythonw "<kit>\reserve_ui.pyw"`.
  - Other desktop shortcuts that still run the old kit or `reserve_shortcut.ps1` are noted, not removed.
- **Agent temp init.** `kitlib/machinesetup/agenttemp.py`. The folder is `--agent-temp`, else the machine config's `agent_temp_sweep.dir` (docs/kit-json.md), else the user environment's `AGENT_TEMP`, else `AgentTemp` at the root of the kit's drive (`default_agent_temp`; a kit on a UNC path gets a note and no folder). A folder that is not absolute, or is a link or a file, is refused. So is every folder the sweep refuses, through the one function both use (`unsafe_dir`): a drive root; the home folder, a folder in it or one holding it; the kit or the machine config folder, a folder in one or one holding one; a folder inside a git checkout or worktree (a `.git` in it or above it). Each check is made on the path as written and on its real path, so a junction onto one of these is refused too.
  - `python tools/agent_temp_init.py [--agent-temp DIR] [--kit DIR] [--home DIR] [--register-sweep-task] [--apply]` runs only these temp-folder steps (the folder, `AGENT_TEMP`, the config key, the sweep task and the permissions below), dry run by default (`run_agent_temp`, `kitlib/machinesetup/__init__.py`). Exit codes: 0 done, dry run printed or nothing to do; 1 refused or stopped; 2 usage.
  - The folder is made when it is missing; the permissions step below then plans its grant without reading `icacls`.
  - The user environment variable `AGENT_TEMP` is set to the folder when it is unset. Set to another folder, it is set only when `--agent-temp` was given; otherwise the plan notes it and leaves it.
  - The machine config gets `agent_temp_sweep = {"dir": <folder>, "max_age_days": 3}` when it has no such key, the file's other keys and layout kept (`write_sweep_key`). The write refuses a file that changed since the survey, and puts the file back when the config cannot be read after it. An existing key is noted and never overwritten. A machine config only in the retired folder is refused (`kitlib.config.user.writable_file`).
- **Agent temp sweep task.** The scheduled task `agent-temp-sweep` runs `python "<kit>\optional\agenttemp\agent_temp_sweep.py" --apply` daily at 04:00 as the registering user (`-RunLevel Limited`, `-StartWhenAvailable`; `task_line`). It opens no console window: the task's program is the `pythonw.exe` that `Get-Command pythonw` finds when the line runs, with the arguments `"<kit>\tools\no_window.py" python "<kit>\optional\agenttemp\agent_temp_sweep.py" --apply` (the carrier, docs/daily-full.md "The schedule"), and the line throws, registering nothing, when there is no `pythonw` or it is the Microsoft Store alias. Registering it is the owner's act: only `--register-sweep-task` (`-RegisterSweepTask`) with `--apply` registers it; without the flag the plan notes the PowerShell line that would. A task already registered is left, so one registered before the carrier keeps running `python` with a window until the owner undoes it (below) and registers it again; a `schtasks` query that fails or takes over 30 s is a note (`task_registered`). Undo with `Unregister-ScheduledTask -TaskName agent-temp-sweep -Confirm:$false`.
  - What the sweep removes (its docstring, `optional/agenttemp/agent_temp_sweep.py`): top-level entries of the folder, and under `claude/` each `claude/<project>/<session>` folder Claude Code keeps a session's scratch in, once the newest time in it is older than `max_age_days` and it can be renamed aside (an entry with an open file or a working folder in it cannot). `agent_temp_sweep.keep` lists entries it never removes (`claude/<project>/<session>`, `*` and `?` within one name; case-insensitive; a keep entry deeper than the entry the sweep removes, such as `kit-coordinator/r2`, keeps that whole entry (`is_kept`)). Under `claude/` it also keeps an entry whose name is in a running process's command line; a Claude Code process normally carries no session id there (only `--resume <id>` or `--session-id <id>` does), so this rarely keeps one. When the process list cannot be read within 30 s it keeps every `claude/` entry, prints one line and exits 3. A project folder that vanished or cannot be listed is skipped with one line and the rest goes on. So is a `claude/` folder that cannot be listed. When both apply, exit 3 wins over exit 1.
  - Remaining risk: a session idle, with no open handle in its folder, for longer than `max_age_days` loses its scratch; the keep-list is the remedy. An empty folder some tool left (a dotnet build's, say) goes like any other old entry.
  - Run it once by hand without `--apply` and read the list before registering the task.
- **Agent temp permissions.** On the folder above.
  - The command is `icacls <dir> /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F <DOMAIN\user>:(OI)(CI)F`. That leaves other explicit entries, so each one (and any deny entry) is then removed with `icacls <dir> /remove:g <who>` (`/remove:d` for a deny); the plan names them.
  - It runs unless the folder already has no inherited entries, no deny entries, and only SYSTEM, Administrators and the user with full control. Undo with `icacls <dir> /reset`.
  - An empty `USERDOMAIN` or `USERNAME` is refused. The parse reads English `icacls` output.
- **Comfy lock tool.** The user environment variable `KIT_MACHINE_LOCK` = `<kit>\tools\machine_lock.py`.
  - `optional/comfy/comfy_gen.py` otherwise looks two folders above itself, which is `<config>/tools/machine_lock.py` through the link and does not exist.

## Fresh machine (--fresh)

`--fresh` (`-Fresh`) is for a machine with no older kit clone. It cannot be given with `--old-kit`. It only appends: no entry is removed, re-pointed or rewritten into exec form. The dry run prints the sections links, settings, plugin, extras (`Plan.FRESH_SECTIONS`, `kitlib/machinesetup/common.py`). `--apply` makes the changes in the order the survey adds them (`survey`, `kitlib/machinesetup/__init__.py`): the settings writes, the PM hook, the plugin, the extras.

- **Links.** None are switched; the plan notes `fresh: no older kit clone, no links switched`.
- **Settings.** In each config folder's `settings.json`, the guard entry `boot/hook.py claude --settings-entry` prints is appended when no PreToolUse hook names `<kit>/boot/hook.py`. A missing `settings.json` is created, with indent 2, LF and a final newline; the dry run shows it as a diff from nothing, and `--apply` refuses to replace a file that appeared since the survey. An existing file keeps its layout and gets the `.bak` described in "Settings".
- **PM SessionStart hook.** It is written only to the file `--pm-settings` (`-PmSettings`) names: the settings the PM session loads, for example `<PM repo>/.claude/settings.local.json` (docs/pm-start.md "Hook (settings.json)"). The hook runs `boot/hook.py claude SessionStart pm` in exec form with the guard entry's interpreter and the matcher `startup|resume|compact`. Without `--pm-settings` it is a note, not a change. It refuses a file that is an account's `settings.json`, a missing folder, and a file whose SessionStart hook already runs a `pm_session_start.py` other than this kit's. It also refuses when a `pm_session_start.py` hook, under any event, is already in the other file of the pair `settings.json` and `settings.local.json` next to it, or in any config folder's `settings.json`: the PM checklist would run twice (`_pm_hook_elsewhere`, `kitlib/machinesetup/settings.py`).
- **Plugin.** The plugin is installed from the export of the kit's pushed main, never from the checkout (docs/plugin-source.md), the way `tools/plugin_refresh.py --migrate --apply` does it (`kitlib/machinesetup/plugin.py`). A config folder needs it when its `settings.json` has no `enabledPlugins["pm-kit@pm-kit"]`, or has `true` but the export's `state.json` does not record the folder as migrated (an install from the checkout, which `plugin_refresh.py --apply` refuses); the second case is noted. One change makes the export (`ensure_export`, `kitlib/pluginsource/__init__.py`; it runs no `claude` and refuses when the kit checkout has not fetched its remote's main). Then one change per folder runs `refresh` with migrate for that folder: `claude plugin marketplace remove pm-kit` (its failure tolerated), `marketplace add <export root>` and `install pm-kit@pm-kit -s user` through `Runner` (`kitlib/pluginsource/claude.py`), which removes `CLAUDE_CONFIG_DIR` for `~/.claude` and sets it to any other folder. `machine_setup.py --apply` gives `Runner` `run_tree` (`kitlib/machinesetup/extras.py`), which starts `claude` in a job object so a timeout ends its children too. The CLI also rewrites other keys of `settings.json`, so the change first copies it to `settings.json.machine-setup-<yyyymmdd>-plugin.bak`, and afterwards checks the entry is `true`; if a step fails or the entry is not `true`, the run stops there, and the undo text gives the folder's `--migrate --folder` command to run it again. An entry that is `false` or any other value is noted and left. With no `claude` on PATH each folder is a note giving that command. A `--config-dir` folder outside `account_folders` is never installed: `plugin_refresh.py` and `pm_start` would never update it, so it is a note giving the same three `claude` commands and the two update commands to run there by hand, and the export change is still made for it (`_split`, `_outside_note`, `kitlib/machinesetup/plugin.py`). It runs after the settings writes, so the `.bak` holds the guard entry. The export folder comes from the machine config's `plugin` key, by default under `%LOCALAPPDATA%` (docs/plugin-source.md).
- **Extras.** The shortcut, the agent temp init, the sweep task and the agent temp permissions run as in "Extras". `KIT_MACHINE_LOCK` is not set: the plugin's lock tool is used, and the plan notes it.

## Not done here

- **Other old-kit links.** For example `agents` and `hooks` are noted and left: the plugin replaces agents (go-live.md "Switches"), and the launcher starts the agent `launcher.coordinator_agent` names (docs/launcher.md "Settings").
- **The plugin install, with `--old-kit`.** `machine_setup.py --old-kit` installs no plugin (`--fresh` does, in "Fresh machine (--fresh)"). `tools/plugin_refresh.py --migrate --apply` adds the export of pushed kit main as the `pm-kit` marketplace in every account folder and installs the plugin from it; `--folder DIR` migrates one folder first (docs/plugin-source.md "Migrating").
- **Scheduled tasks.** Ones that run old-kit scripts are not scanned. Only `agent-temp-sweep` is registered, and only with `--register-sweep-task`.
- **Skills as a plugin.** go-live.md "Skills, templates, optional tools, knowledge" is a separate switch. These links keep skills loading until the plugin is enabled. Remove them then, or each skill appears twice (docs/install.md, "After the plugin install").

## Code and tests

| Module | Holds |
|---|---|
| `kitlib/machinesetup/__init__.py` | `Context`, `default_config_dirs`, `dedupe`, `survey`, `run` (exit code and lines) |
| `kitlib/machinesetup/common.py` | `Plan`, `Change`, link and path helpers |
| `kitlib/machinesetup/links.py`, `settings.py`, `extras.py` | the three parts above; `settings.py` also holds the fresh-mode append and the PM hook (`survey_pm`) |
| `kitlib/machinesetup/plugin.py` | the fresh-mode plugin step |
| `kitlib/machinesetup/agenttemp.py` | the agent temp init and the sweep task (`default_agent_temp`, `task_line`, `write_sweep_key`, `unsafe_dir`, `task_registered`), called from `extras.py` |
| `tools/machine_setup.py`, `installer/machine-setup.ps1` | the entry point and its PowerShell wrapper |
| `tools/agent_temp_init.py` | the entry point for the agent temp steps alone |
 `tests/machinesetup/test_moved.py` covers "Moved hook paths": the re-pointing in each mode and the PM settings, the skill links (Windows), and the stubs.
`tests/machinesetup/test_machinesetup.py` builds a fake home, a fake old kit and a fake kit in a temp folder. It runs a dry run, then `--apply`, then a second run, and checks each refusal. `tests/machinesetup/test_fresh.py` does the same for `--fresh`, with a kit that is a temp git repository with a bare remote (so the export is made for real) and a fake `subprocess.run` under the real `Runner`; its `RealRunner` tests run `run_tree` against fake `claude` `.cmd` files for the arguments, the environment, the return code and the timeout. Neither touches the real home folder, desktop, environment or permissions. Every Python they start is `sys.executable`, and the wrapper gets it as `-Python`; `test_no_python_alias_is_started` runs the wrapper and a dry run with a temp `LOCALAPPDATA` on a PATH whose `python`, `pythonw`, `py` and `python3` are fakes that leave a mark, and checks none was started. `tests/machinesetup/test_agent_temp_init.py` covers the agent temp init and the sweep task against the same fake home and `FakeHost` (its `task_exists` and `task_register` stand in for the task scheduler). One test checks, by size and modification time only, that the real accounts' `settings.json` files are unchanged after a fresh `--apply` against a temp home.
