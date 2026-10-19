# Machine setup

One reviewed command sets a machine up for the kit. It writes nothing into any repository (the one exception is opt-in: the `--pm-settings` file you name, a local settings file the PM repository keeps out of its commits): a repository works with the kit with nothing committed in it, and the kit's first use there writes only local git settings (docs/install.md "What first use writes"). Nothing has run it on a real machine yet.

```
powershell -NoProfile -File installer\machine-setup.ps1 [-OwnerEmail <email>] [-PmSettings <file>] [-ConfigDir <folder>,<folder>] [-AgentTemp <folder>] [-NoSweepTask] [-Shortcut] [-Procwatch [-ProcwatchKill]] [-Apply]
powershell -NoProfile -File installer\machine-setup.ps1 -OldKit <old kit clone> [-OwnerEmail <email>] [-ConfigDir <folder>,<folder>] [-AgentTemp <folder>] [-NoSweepTask] [-Procwatch [-ProcwatchKill]] [-Apply]
python tools/machine_setup.py [--old-kit DIR] [--owner-email EMAIL] [--pm-settings FILE] [--config-dir DIR]... [--agent-temp DIR] [--no-sweep-task] [--shortcut] [--procwatch [--procwatch-kill]] [--kit DIR] [--home DIR] [--desktop DIR] [--apply]
python tools/machine_setup.py --repo PATH --owner-email EMAIL --agent-email EMAIL [...] [--apply]
```

Three modes:

- **The default** ("Default mode" below): the guard's entry in each Claude account's `settings.json`, the kit plugin on each host machine setup installs, the owner's email in the machine config ("Owner email") and the agents' temp folder ("Extras").
- **`--old-kit DIR`** (`-OldKit`): switches a machine's Claude config folders from an older kit clone to this kit checkout: each account's `settings.json` (with every link the clone's setup left reported, "Links"), the owner's email and the machine extras (the schedule shortcut, the agents' temp folder with its sweep, and `KIT_MACHINE_LOCK`). It is the switch-over command (docs/go-live.md "Switches").
- **`--repo PATH`**: the committed repo setup, for a repository that wants its kit settings committed: `.kit.json`, the ignore lines, a starter `docs/INDEX.md`, and with `--stack` and `--stubs` their files ("Committed repo setup (--repo)" below; docs/install.md "Committed setup (--repo)"). `installer/new-repo.ps1` is its PowerShell front end.

Run it from the kit's main checkout, not a worktree: `--kit` defaults to the checkout the tool is in, and every hook path and shortcut names that folder.

- **Dry run by default.** It prints every change, with each `settings.json` change as a unified diff, and changes nothing. `--apply` (`-Apply`) makes the same changes, in the printed order.
- **Config folders.** The default mode uses the account folders `tools/plugin_refresh.py` runs in: `~/.claude`, every `~/.claude-*` folder except the machine config's own, current and retired, and each existing folder the launcher config names; the dry run notes that a backup copy such as `~/.claude-backup` is among them unless `--config-dir` leaves it out. `--old-kit` uses the first two only. `--config-dir` (repeatable) names them instead. A folder that is a link to one already listed is read once. With no config folder at all, both modes refuse.
- **Idempotent.** A second run prints `nothing to do` in every section and exits 0.
- **Refusals.** Any refusal stops the run before anything changes: an old-kit path in a settings file that this step neither removes nor re-points, a hook path it cannot resolve, a settings file it cannot read or must not replace, a machine config it cannot read, a kit file a path would need, a missing config folder, or `--kit` equal to `--old-kit`. Each refusal names the file and path.
- **Part-way failure.** If a change fails under `--apply`, whatever the error, it stops there. It prints the change, the error, how to restore that change by hand, and the changes already made. Every line is printed as it is produced.

Exit codes: 0 done, dry run printed or nothing to do; 1 refused or stopped; 2 usage: both `--old-kit` and `--fresh`, `--pm-settings` with `--old-kit`, a `--repo` option without `--repo` or a machine option with it. `--repo` has its own codes (docs/install.md).

The wrapper runs the tool under `-Python <python.exe>` when given, else under the `python` on PATH.

## Links

`kitlib/machinesetup/links.py`. Machine setup makes no link, in either mode. The kit runs from its own folder (the plugin root), so an account needs no `tools`, `knowledge` or `skills` entry pointing into it. This step only reports the links an earlier setup left, and adds no change, so a second run still prints `nothing to do`.

Each leftover is one note, with the command that removes the link only, never its target (never `/s`):

```
leftover link <path> -> <target>: remove it with: cmd /c rmdir "<path>"
```

That is the command for a junction or a directory symlink. A file symlink (one without the directory attribute) gets `cmd /c del "<path>"` instead, since `rmdir` fails on it; `del` removes the link and leaves its target.

In each config folder it checks `tools`, `knowledge` and `skills`. A link counts when its target is in this kit or, with `--old-kit`, in the older kit clone:

- A container that is itself such a link: one leftover note for the container. For `skills`, each skill under its target that the kit has no folder for in `content/skills` is named too: a repo-local skill moves to its repo before the link goes.
- A link whose target is outside this kit (and, with `--old-kit`, the older clone) is left alone but never silently: a container that is a link elsewhere, for example an account folder following `~/.claude`, or an entry of a real `tools`, `knowledge` or `skills` folder that is, gets one note. In the default mode a link an earlier setup made into a kit clone that has moved since is one of these, so the note carries the same remove command:

  ```
  skipped link <path> -> <target>: its target is outside this kit, left as is; if an earlier setup made it, remove it with: cmd /c rmdir "<path>"
  ```

  An absent container is left alone with no note.
- In a real `tools` or `knowledge` folder, each entry that is such a link is a leftover. Earlier setups linked `machine_lock` -> `<kit>/tools`, `kitlib` -> `<kit>/kitlib`, `comfy`, `procwatch`, `usage`, `agenttemp` -> `<kit>/optional/<name>` and `knowledge/asset-generation`. A real folder at one of those names is noted ("a real folder, left as it is") with the kit's own copy; nothing is renamed or deleted.
- In a real `skills` folder, a link at the name of a kit skill is a leftover, including one into a folder the skills have left ("Moved hook paths"); the plugin loads those skills. A link at another name is noted and left: the kit has no such skill.
- With `--old-kit`, any other entry of the config folder (`agents`, `hooks`) that links into the older kit clone is noted and left ("Not done here").

A settings path that runs through one of these links is re-pointed to the kit's own file in the same run ("Settings"), so the owner can remove the links once that change is applied.

## Settings

`kitlib/machinesetup/settings.py`. In each config folder's `settings.json`, it reads every path in a hook's `command` and `args` and in `statusLine.command`. `~`, `%USERPROFILE%`, `$env:USERPROFILE`, `$HOME`, `${HOME}`, `$env:HOME`, `%HOMEDRIVE%%HOMEPATH%` and `%HOMEPATH%` are read as the home folder.

A path it cannot resolve to an absolute one (a relative path, a script name, another variable) is refused, since it cannot tell whether it is the old kit's. Words, switches such as `/c`, URLs and paths under `$CLAUDE_PROJECT_DIR` or `$CLAUDE_PLUGIN_ROOT` are not refused. A `hooks` value, event or group of a shape Claude Code does not use is refused too.

A path that resolves into the old kit, into `<kit>/hooks` or to `<kit>/tools/guard.py` (the pre-move paths, go-live.md "Switches") gets one of three outcomes:
- **Removed.** `no_root_search.py` and `main_checkout_redirect_guard.py`: the hook goes. A group or event that removal empties goes too; one that was already empty stays.
- **Re-pointed.** The file is rewritten to the kit's copy with forward slashes, quotes kept:
  - `pm_session_start.py` and `coordinator_session_start.py` become the kit's hook entry: `boot/hook.py claude SessionStart pm` and `boot/hook.py claude SessionStart coordinator` (the words follow the path in a command line, or are added to `args` after it).
  - `statusline-usage.ps1` and `write-usage-status.ps1` go to `optional/usage/`.
- **Refused.** Any other old-kit path, named.

A path through `<config>/tools/<name>`, a name in `links.TOOL_LINKS`, is re-pointed to the kit file that link reached, forward slashes, quotes kept (an unquoted path that gains a space is quoted): `<config>/tools/usage/statusline-usage.ps1` becomes `<kit>/optional/usage/statusline-usage.ps1`. `<config>` is any config folder of the run or of the home, since a status line can name another account's folder. This runs in every mode, in hooks (`command` and `args`) and `statusLine.command`, and in the `--pm-settings` file. It is refused, named, when the kit has no such file.

When no PreToolUse hook names `<kit>/boot/hook.py`, the entry `boot/hook.py claude --settings-entry` prints is appended (docs/guard.md). The host, `claude`, is hosts.json's default host, whose account settings these are.

A SessionStart hook in shell form whose command is a Python interpreter (`python`, `python.exe`, `pythonw`, `pythonw.exe`, by base name), then `<kit>/boot/hook.py` and its words (at most three plain words; a redirect or a pipe keeps the hook in shell form), is written in exec form: `command` becomes the interpreter of the guard entry `boot/hook.py claude --settings-entry` prints, and `args` holds the script with forward slashes, then its words. Exec form starts no shell. Any other command, a hook that already has `args`, and every other event are left as they are.

When the kit's `.claude-plugin/plugin.json` names a `hooks` file, a settings hook that runs the same script with the same words as one of that file's hooks, for the same event, is noted (`${CLAUDE_PLUGIN_ROOT}` read as the kit): once the plugin is enabled the hook runs twice, so the settings entry is removed in the same step as the plugin install (docs/install.md "Plugin install"). The note changes nothing.

`statusLine` stays in shell form: Claude Code gives it no `args` field.

Each of these is a planned change or a note: a dry run writes nothing, and `--apply` writes through the backup below.

The file is edited as JSON and written back in its own layout: indent, ASCII escaping, CRLF, BOM and final newline. Only the changed lines differ. A layout `json.dumps` cannot reproduce is noted, and the diff then shows the whole rewrite.

A `settings.json` that is a symbolic link or has more than one hard link is refused when it needs a change: writing through a temp file would split it from the file it shares.

`--apply` refuses if the file changed since the survey or has become a link. It then copies the file to `settings.json.machine-setup-<yyyymmdd>.bak` and writes through a temp file, which it removes if the write or the rename fails.

### Moved hook paths

Every hook runs the kit's one entry, `<kit>/boot/hook.py <host> <event> [<part>]`; the per-script hooks in `<kit>/claude/hooks/` and `<kit>/hosts/claude/shell/hooks/` are gone, and the skills are in `<kit>/content/skills/`. Settings and links written before still name the old paths, so:

- **Settings.** Before any other step, every hook path to a retired script in those folders is re-pointed to the entry (`settings.repoint_moved`): `guard.py` becomes `boot/hook.py claude PreToolUse`, `pm_session_start.py` `boot/hook.py claude SessionStart pm`, `coordinator_session_start.py` `boot/hook.py claude SessionStart coordinator`. This runs in every mode and in the `--pm-settings` file. Any other script name in those folders, or a kit with no `boot/hook.py`, is refused.
- **Skill links.** A link into `<kit>/claude/skills/` or `<kit>/hosts/claude/shell/skills/` is a leftover like any other ("Links").
- **No stubs.** An account whose settings still name a retired script blocks every guarded tool call (Python's `can't open file '<kit>/.../guard.py'`), so the owner re-runs machine setup once on each machine (dry run, then `--apply`, in its mode; go-live.md "Switches"), from a terminal, not from a Claude session, then restarts the open sessions. Under `--old-kit`, a PM repo's own settings file that still runs `pm_session_start.py` is edited by hand to the hook in docs/pm-start.md.

## Extras

`kitlib/machinesetup/extras.py`. Each extra is read first and planned only when it differs.

- **Machine schedule shortcut.** `Machine schedule.lnk` on the desktop (or `--desktop`) runs `pythonw "<kit>\reserve_ui.pyw"`. With `--old-kit` always; in the default mode only with `--shortcut` (`-Shortcut`), and the plan notes it otherwise.
  - Other desktop shortcuts that still run the old kit or `reserve_shortcut.ps1` are noted, not removed.
- **Agent temp init.** `kitlib/machinesetup/agenttemp.py`. The folder is `--agent-temp`, else the machine config's `agent_temp_sweep.dir` (docs/kit-json.md), else the user environment's `AGENT_TEMP`, else `kit-temp` at the root of the kit's drive. A folder that is not absolute, or is a link or a file, is refused. So is every folder the sweep refuses, through the one function both use: a drive root; the home folder, a folder in it or one holding it; the kit or the machine config folder, a folder in one or one holding one; a folder inside a git checkout or worktree (a `.git` in it or above it). Each check is made on the path as written and on its real path, so a junction onto one of these is refused too.
  - `python tools/agent_temp_init.py [--agent-temp DIR] [--kit DIR] [--home DIR] [--no-sweep-task] [--apply]` runs only these temp-folder steps (the folder, `AGENT_TEMP`, the config key, the sweep task and the permissions below), dry run by default. Exit codes: 0 done, dry run printed or nothing to do; 1 refused or stopped; 2 usage.
  - The folder is made when it is missing; the permissions step below then plans its grant without reading `icacls`.
  - The user environment variable `AGENT_TEMP` is set to the folder when it is unset. Set to another folder, it is set only when `--agent-temp` was given; otherwise the plan notes it and leaves it.
  - The machine config gets `agent_temp_sweep = {"dir": <folder>, "max_age_days": 3}` when it has no such key, the file's other keys and layout kept. The write refuses a file that changed since the survey, and puts the file back when the config cannot be read after it. An existing key is noted and never overwritten. A machine config only in the retired folder is refused.
- **Agent temp sweep task.** The scheduled task `agent-temp-sweep` runs `python "<kit>\optional\agenttemp\agent_temp_sweep.py" --apply` daily at 04:00 as the registering user, through the carrier and the base install's `pythonw.exe` (docs/daily-full.md "The schedule"), so it opens no console window; the line throws, registering nothing, when that `pythonw` is not a file or is under `WindowsApps`. `--apply` registers it by default. It is two planned changes: first the sweep runs once without `--apply` and prints each line of its report, prefixed `sweep dry run (nothing removed):`, so the run shows what the task's first run would remove; a dry run that refuses or fails stops the run there, registering nothing. `--no-sweep-task` (`-NoSweepTask`) leaves the task unregistered and notes the line that would register it. A task already registered is left as is. Undo with `Unregister-ScheduledTask -TaskName agent-temp-sweep -Confirm:$false`.
  - What the sweep removes (its docstring, `optional/agenttemp/agent_temp_sweep.py`): top-level entries of the folder, and under `claude/` each `claude/<project>/<session>` folder, once the newest time in it is older than `max_age_days` and it can be renamed aside (an entry with an open file in it cannot). `agent_temp_sweep.keep` lists entries it never removes. When the process list cannot be read within 30 s it keeps every `claude/` entry and exits 3.
  - Remaining risk: a session idle, with no open handle in its folder, for longer than `max_age_days` loses its scratch; the keep-list is the remedy. An empty folder some tool left (a dotnet build's, say) goes like any other old entry.
  - The default `max_age_days` stays 3: the age, the claim and the keep-list carry the safety, and the dry run printed before registering is the list to read. To read it before any task exists, apply with `--no-sweep-task`, run `python <kit>\optional\agenttemp\agent_temp_sweep.py` by hand, then apply again without the flag.
- **Procwatch task** (opt-in). `kitlib/machinesetup/procwatch.py`. `--procwatch` (`-Procwatch`) registers the scheduled task `Claude procwatch`: every 15 minutes it runs `python "<kit>\optional\procwatch\procwatch.py" --log "<agent temp>\procwatch.log"` through the carrier, as the sweep task above does; with no agents' temp folder it is skipped with a note. The task only reports; `--procwatch-kill` (`-ProcwatchKill`; a usage error without `--procwatch`) appends `--kill --only` with the kill-allowlisted rules (`SAFE`). The line is `Register-ScheduledTask -Force`, so a task already there is replaced by the kit's definition. Undo with `Unregister-ScheduledTask -TaskName 'Claude procwatch' -Confirm:$false`; to keep the old definition, `Export-ScheduledTask` it first.
- **Agent temp permissions.** On the folder above.
  - The command is `icacls <dir> /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F <DOMAIN\user>:(OI)(CI)F`. That leaves other explicit entries, so each one (and any deny entry) is then removed with `icacls <dir> /remove:g <who>` (`/remove:d` for a deny); the plan names them.
  - It runs unless the folder already has no inherited entries, no deny entries, and only SYSTEM, Administrators and the user with full control. Undo with `icacls <dir> /reset`.
  - An empty `USERDOMAIN` or `USERNAME` is refused. The parse reads English `icacls` output.
- **Comfy lock tool.** The user environment variable `KIT_MACHINE_LOCK` = `<kit>\tools\machine_lock.py`.

## Default mode

The default (no `--old-kit`) is for any machine with no older kit clone, a new one or one set up before. It only appends: no entry is removed or rewritten into exec form, and the only rewrites are a hook path to a retired kit hook script ("Moved hook paths") and a path through a tools link ("Settings"). The dry run prints the sections links, settings, plugin, owner, extras. `--apply` makes the changes in the order the survey adds them: the settings writes, the PM hook, the plugin, the owner's email, the extras.

- **Links.** None are made; each leftover link into the kit is reported as in "Links".
- **Settings: the guard's entry.** In each config folder's `settings.json`, the guard entry `boot/hook.py claude --settings-entry` prints (`boot/hook.py claude PreToolUse`) is appended when no PreToolUse hook names `<kit>/boot/hook.py`. It stays there until the plugin carries the guard (docs/guard.md "Go-live"). A missing `settings.json` is created, with indent 2, LF and a final newline; the dry run shows it as a diff from nothing, and `--apply` refuses to replace a file that appeared since the survey. An existing file keeps its layout and gets the `.bak` described in "Settings".
- **PM SessionStart hook** (opt-in). It is written only to the file `--pm-settings` (`-PmSettings`) names: the settings the PM session loads, for example `<PM repo>/.claude/settings.local.json` (docs/pm-start.md "Hook (settings.json)"), a file that repository's `.gitignore` or `info/exclude` keeps out of its commits. The hook runs `boot/hook.py claude SessionStart pm` in exec form with the guard entry's interpreter and the matcher `startup|resume|compact`. Without `--pm-settings` it is a note, not a change. It refuses a file that is an account's `settings.json`, a missing folder, and a file whose SessionStart hook already runs a `pm_session_start.py` other than this kit's. It also refuses when a PM hook is already in another settings file: the PM checklist would run twice.
- **Plugin**, per host. For each host in `hosts.json`, in order, the host's install step, or a note for a host machine setup does not install. Claude has one (below). Codex has none: `hosts/codex/README.md` says nothing of it is installed or activated on any machine, and its agents and skills are review templates, not installed, so the plan notes `codex: not installed` and names that README. A host gains a step in `installer` once a live install of its plugin is verified.
- **Plugin: Claude.** The plugin is installed from the export of the kit's pushed main, never from the checkout (docs/plugin-source.md), the way `tools/plugin_refresh.py --migrate --apply` does it. A config folder needs it when its `settings.json` has no `enabledPlugins["pm-kit@pm-kit"]`, or the export does not record the folder as migrated. One change makes the export (it refuses when the kit checkout has not fetched its remote's main); then one change per folder runs `claude plugin marketplace remove pm-kit`, `marketplace add <export root>` and `install pm-kit@pm-kit -s user`. The change first copies `settings.json` to `settings.json.machine-setup-<yyyymmdd>-plugin.bak` and afterwards checks the entry is `true`; if not, the run stops and the undo text gives the folder's `--migrate --folder` command. With no `claude` on PATH, or a `--config-dir` folder outside the account folders, each folder is a note giving the commands to run by hand.
- **Owner email.** "Owner email" below.
- **Extras.** The agent temp init, the sweep task (unless `--no-sweep-task`) and the agent temp permissions run as in "Extras"; the shortcut only with `--shortcut`. `KIT_MACHINE_LOCK` is not set: the plugin's lock tool is used, and the plan notes it.

## Owner email

`kitlib/machinesetup/owner.py`, in both machine modes. The machine config's `owner_email` (docs/kit-json.md "Machine config keys") is the owner's identity for every repository whose `.kit.json` sets no `"owner".email`; with neither, no commit can be proven to be the owner's and every owner-only path refuses every edit (docs/kit-json.md "With no file").

- `--owner-email` (`-OwnerEmail`): stored when the machine config holds another value or none; the change names the value it replaces, and its undo text sets it back.
- None given and one stored: kept, nothing to do.
- None given and none stored: a note, and nothing is stored on its own. `git config user.email`, read in the kit checkout, is only suggested: the note names it and says to confirm it with `--owner-email <it>`. A git identity can be any address, an agent's among them, so it is never stored unconfirmed. With `--apply` in a console (stdin and stdout a terminal), `tools/machine_setup.py` asks first: `y` stores the suggested address, a typed address stores that, and an empty answer or `n` stores nothing. With no console (a redirected run), nothing is asked and nothing stored.
- A value that is not an email address is refused when given, and a note when found in git config.
- Only the owner runs it to act: the guard's `owner-setup` rule denies `machine_setup.py --apply` (and its front ends' `-Apply`) to every other session, and the write jail denies an agent session's edit of the machine config (docs/guard.md "Owner machine setup").

The write keeps the machine config's other keys and its layout (`kitlib/machinesetup/machineconfig.py`, shared with the sweep key below): it refuses a file that changed since the survey, and puts the file back when the machine config cannot be read after it. A machine config only in the retired folder is refused.

## Committed repo setup (--repo)

`kitlib/machinesetup/repo.py`; `tools/machine_setup.py --repo PATH --owner-email EMAIL --agent-email EMAIL [--main-branch NAME] [--repo-name NAME] [--stack NAME] [--stubs] [--pm-data DIR [--private-remote URL]] [--apply]`. `installer/new-repo.ps1` is its PowerShell front end (`-Target` is `--repo`). It writes into the repository and nothing on the machine; no machine option goes with it. What it writes, refuses and prints is docs/install.md "Committed setup (--repo)".

## Not done here

- **Removing links.** Every leftover link is reported with its removal command ("Links") and left for the owner to remove. Other links into the older kit clone, for example `agents` and `hooks`, are noted and left: the plugin replaces agents (go-live.md "Switches"), and the launcher starts the agent `launcher.coordinator_agent` names (docs/launcher.md "Settings").
- **The plugin install, with `--old-kit`.** `machine_setup.py --old-kit` installs no plugin (the default mode does, in "Default mode"). `tools/plugin_refresh.py --migrate --apply` adds the export of pushed kit main as the `pm-kit` marketplace in every account folder and installs the plugin from it; `--folder DIR` migrates one folder first (docs/plugin-source.md "Migrating").
- **Scheduled tasks.** Ones that run old-kit scripts are not scanned. Only `agent-temp-sweep` is registered (by default with `--apply`; not with `--no-sweep-task`), and, in the default mode and the old-kit mode alike, the procwatch task when `--procwatch` is given ("Procwatch task" in Extras).
- **Skills as a plugin.** go-live.md "Skills, templates, optional tools, knowledge" is a separate switch. The plugin loads the kit's skills; a leftover skill link loads each one a second time, so it goes once the plugin is enabled (docs/install.md, "After the plugin install").

## Code and tests

| Module | Holds |
|---|---|
| `kitlib/machinesetup/__init__.py` | `Context`, `default_config_dirs`, `dedupe`, `survey`, `run` (exit code and lines), `run_repo` |
| `kitlib/machinesetup/common.py` | `Plan`, `Change`, link and path helpers |
| `kitlib/machinesetup/links.py`, `settings.py`, `extras.py` | the three parts above: the leftover-link report (`survey`, `leftover`, `TOOL_LINKS`), the settings edits (`repoint_links`, `repoint_moved`), the extras; `settings.py` also holds the default mode's append and the PM hook (`survey_pm`) |
| `kitlib/machinesetup/plugin.py` | the default mode's plugin step, per host (`survey_hosts`, `installer`) |
| `kitlib/machinesetup/owner.py`, `machineconfig.py` | the owner's email; the machine config writes it shares with the sweep key |
| `kitlib/machinesetup/repo.py` | the committed repo setup (`--repo`, `installer/new-repo.ps1`) |
| `kitlib/machinesetup/agenttemp.py` | the agent temp init and the sweep task (`default_agent_temp`, `task_line`, `write_sweep_key`, `unsafe_dir`, `task_registered`), called from `extras.py` |
| `tools/machine_setup.py`, `installer/machine-setup.ps1`, `installer/new-repo.ps1` | the entry point and its two PowerShell wrappers |
| `tools/agent_temp_init.py` | the entry point for the agent temp steps alone |

Tests: `tests/machinesetup`.
