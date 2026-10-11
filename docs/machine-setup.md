# Machine setup

One reviewed command switches a machine's Claude config folders from an older kit clone to this kit checkout: the links, each account's `settings.json`, and three machine extras. It is the switch-over command for the joint review (docs/go-live.md "Switches"). Nothing has run it on a real machine yet.

```
powershell -NoProfile -File installer\machine-setup.ps1 -OldKit <old kit clone> [-ConfigDir <folder>,<folder>] [-AgentTemp <folder>] [-Apply]
python tools/machine_setup.py --old-kit DIR [--config-dir DIR]... [--agent-temp DIR] [--kit DIR] [--home DIR] [--desktop DIR] [--apply]
```

Run it from the kit's main checkout, not a worktree: `--kit` defaults to the checkout the tool is in, and every link, hook path and shortcut names that folder.

- **Dry run by default.** It prints every change, with each `settings.json` change as a unified diff, and changes nothing. `--apply` (`-Apply`) makes the same changes, in the printed order.
- **Config folders.** Defaults to `~/.claude` and every `~/.claude-*` folder except the machine config's own, current and retired (`kitlib.config.user.DIR_NAME`, `kitlib.config.migration.OLD_DIR_NAME`). `--config-dir` (repeatable) names them instead; the wrapper's `-ConfigDir a,b` is split on commas, so a folder name with a comma cannot be passed there. A folder that is a link to one already listed is read once.
- **Idempotent.** A second run prints `nothing to do` in every section and exits 0.
- **Refusals.** Any refusal stops both modes before anything changes: an old-kit path in a settings file that this step neither removes nor re-points, a hook path it cannot resolve, a settings file it cannot read or must not replace, a kit file a link or path would need, a file where a link goes, a missing config folder, or `--kit` equal to `--old-kit`. Each refusal names the file and path.
- **Part-way failure.** If a change fails under `--apply`, whatever the error, it stops there. It prints the change, the error, how to restore that change by hand, and the changes already made. Every line is printed as it is produced.

Exit codes: 0 done, dry run printed or nothing to do; 1 refused or stopped; 2 usage (the wrapper also exits 2 with no `-OldKit` or no `python` on PATH).

## Links

`kitlib/machinesetup/links.py`. In each config folder it checks `tools`, `knowledge` and `skills`:

- `tools`: holds a junction per entry. `machine_lock` -> `<kit>/tools`, `kitlib` -> `<kit>/kitlib`, and `comfy`, `procwatch`, `usage`, `agenttemp` -> `<kit>/optional/<name>`.
  - It cannot be one junction onto a kit folder. The live lock path is `tools/machine_lock/machine_lock.py`, and the kit's `tools/machine_lock.py` imports `kitlib` from beside the folder it was started from, spelled through the junction (`os.path.abspath` does not resolve it).
- `knowledge/asset-generation` -> `<kit>/knowledge/asset-generation`.
- `skills/<name>` -> `<kit>/claude/skills/<name>`, for each old-kit skill the kit has. One it lacks (a repo-local skill) is noted and keeps pointing at the old kit, linked again when its container is replaced.

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
  - `pm_session_start.py` and `coordinator_session_start.py` go to `claude/hooks/`.
  - `statusline-usage.ps1` and `write-usage-status.ps1` go to `optional/usage/`.
- **Refused.** Any other old-kit path, named.

A path through `<config>/tools/<link>` stays as written: the links step points it at the kit. It is refused when the kit has no file there.

When no PreToolUse hook names `<kit>/claude/hooks/guard.py`, the entry `guard.py --settings-entry` prints is appended (docs/guard.md).

A SessionStart hook in shell form whose command is exactly a Python interpreter (`python`, `python.exe`, `pythonw`, `pythonw.exe`, by base name) and one `.py` file under `<kit>/claude/hooks/` is written in exec form: `command` becomes the interpreter of the guard entry `guard.py --settings-entry` prints, and `args` holds the script with forward slashes. Exec form starts no shell. Any other command, a hook that already has `args`, and every other event are left as they are.

When the kit's `.claude-plugin/plugin.json` names a `hooks` file, a settings hook that runs one of that file's scripts for the same event is noted (`${CLAUDE_PLUGIN_ROOT}` read as the kit): once the plugin is enabled the hook runs twice, so the settings entry is removed in the same step as the plugin install (docs/install.md "Plugin install"). The note changes nothing.

`statusLine` stays in shell form. Claude Code gives it no `args` field: "The `command` field runs in a shell", and "On Windows, Claude Code runs status line commands through Git Bash when Git Bash is installed, or through PowerShell when Git Bash is absent" (https://code.claude.com/docs/en/statusline).

Each of these is a planned change or a note: a dry run writes nothing, and `--apply` writes through the backup below.

The file is edited as JSON and written back in its own layout: indent, ASCII escaping, CRLF, BOM and final newline. Only the changed lines differ. A layout `json.dumps` cannot reproduce is noted, and the diff then shows the whole rewrite.

A `settings.json` that is a symbolic link or has more than one hard link is refused when it needs a change: writing through a temp file would split it from the file it shares.

`--apply` refuses if the file changed since the survey or has become a link. It then copies the file to `settings.json.machine-setup-<yyyymmdd>.bak` and writes through a temp file.

## Extras

`kitlib/machinesetup/extras.py`. Each extra is read first and planned only when it differs. `Host` is the Windows side; tests pass a fake.

- **Machine schedule shortcut.** `Machine schedule.lnk` on the desktop (or `--desktop`) runs `pythonw "<kit>\reserve_ui.pyw"`.
  - Other desktop shortcuts that still run the old kit or `reserve_shortcut.ps1` are noted, not removed.
- **Agent temp permissions.** The folder is `--agent-temp`, else the machine config's `agent_temp_sweep.dir` (docs/kit-json.md), else skipped with a note.
  - The command is `icacls <dir> /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F <DOMAIN\user>:(OI)(CI)F`. That leaves other explicit entries, so each one (and any deny entry) is then removed with `icacls <dir> /remove:g <who>` (`/remove:d` for a deny); the plan names them.
  - It runs unless the folder already has no inherited entries, no deny entries, and only SYSTEM, Administrators and the user with full control. Undo with `icacls <dir> /reset`.
  - An empty `USERDOMAIN` or `USERNAME` is refused. The parse reads English `icacls` output.
- **Comfy lock tool.** The user environment variable `KIT_MACHINE_LOCK` = `<kit>\tools\machine_lock.py`.
  - `optional/comfy/comfy_gen.py` otherwise looks two folders above itself, which is `<config>/tools/machine_lock.py` through the link and does not exist.

## Not done here

- **A fresh machine.** One with no old kit is not handled: `-OldKit` is required (the wrapper exits 2 without it). There, the plugin, guard.py's PreToolUse entry and the PM SessionStart hook are hand steps (docs/pm-first-run.md steps 3, 6 and 7).
- **Other old-kit links.** For example `agents` and `hooks` are noted and left: the plugin replaces agents (go-live.md "Switches"), and the launcher starts the agent `launcher.coordinator_agent` names (docs/launcher.md "Settings").
- **Scheduled tasks.** Ones that run old-kit scripts are not scanned.
- **Skills as a plugin.** go-live.md "Skills, templates, optional tools, knowledge" is a separate switch. These links keep skills loading until the plugin is enabled. Remove them then, or each skill appears twice (docs/install.md, "After the plugin install").

## Code and tests

| Module | Holds |
|---|---|
| `kitlib/machinesetup/__init__.py` | `Context`, `default_config_dirs`, `dedupe`, `survey`, `run` (exit code and lines) |
| `kitlib/machinesetup/common.py` | `Plan`, `Change`, link and path helpers |
| `kitlib/machinesetup/links.py`, `settings.py`, `extras.py` | the three parts above |
| `tools/machine_setup.py`, `installer/machine-setup.ps1` | the entry point and its PowerShell wrapper |

`tests/machinesetup/test_machinesetup.py` builds a fake home, a fake old kit and a fake kit in a temp folder. It runs a dry run, then `--apply`, then a second run, and checks each refusal. It never touches the real home folder, desktop, environment or permissions.
