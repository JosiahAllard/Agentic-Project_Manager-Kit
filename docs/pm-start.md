# PM session-start checklist

`boot/hook.py claude SessionStart pm` (the kit's one hook entry, its `pm` part) prints a checklist at every PM session start (as
`additionalContext`; logic `kitlib/pmstart`). A hook cannot call tools, so it lists steps for the
PM. It always exits 0; a failed check becomes one "could not check X: reason" line.

Checks: standing rules, eng_review flags, the hub, `pm_watch.py` running, the hand-off file ("Hand-off file"), reminders due, and coordinators not running (no `claude.exe`
with the exact, case-sensitive `--remote-control` name, as `kitlib/launcher/lookup.py`); each such line names the
`launch.py start --session <key> --account <n>` that starts it (docs/launcher.md). A repository in sessions.json with no live `consultant-<repo>` session (and no consultant entry of its own) gets one `consultant-<repo> is not running: launch.py consultant --repo <repo> --repo-path <folder>` line, the folder from the machine config's `launcher.repos` or `<its checkout>` when it is not there (`kitlib.pmstart` `_consultants_missing`; no config switch). `<n>` is the account
this PM session runs on (`kitlib.pmstart` `current_account`), not the entry's stored one, so the PM
launches every coordinator, the kit's included, from whatever account it started on; when that
account cannot be identified the line says `--account auto` (pick-account's choice). Tests:
`tests/pmstart/test_pmstart.py` `LivenessTest` (the consultant line too). A coordinator the PM stopped on its `ready to stop:`
reply (skill session-handoff) is listed too; the PM starts it only when its repo gets startable
work, an owner answer or an approval, adding a `--prompt` that names the reply's note.

Hub (`kitlib.pmstart` `hub_site`, tests `tests/pmstart/test_hub_site.py`; docs/action-hub.md): the
lines read the machine config's `hub` key (`kitlib.hubsite.settings`), or every default (port 8790)
when there is no `hub` key. The check only asks the loopback server on `hub.port`, through
`kitlib.hubsite.client`; it starts no process and never starts the server, and it imports
`kitlib.hubsite.serve` only once the settings are read. Each ask (`GET /api/v1/health`,
then `GET /api/v1/pending?for=pm`) waits at most `HUB_SITE_TIMEOUT_S` (1.5 seconds). The pending
ask is the PM's items only, the list `hub.py pending --for pm` prints: an item routed to a creator
that can still be reached is that session's to ack, and a PM ack would hide it from the creator's
wait. The lines:
`hub: server up on port <n>, <k> pending for the PM` (`<k>` is 0 when the answer's `items` is not a
list); when `<k>` is above 0, a line to process the items (`hub.py pending --for pm`); then either
`hub: wait --pending is running` or a warning, `hub: WARN no PM wait --pending --for pm is
running, so items routed to the PM go unseen; ...`, telling the PM to start
`hub.py wait --pending --for pm` in the background and start it again after it ends (docs/action-hub.md
"Who handles a pending item"). "Running" is matched by
command text only: any `python` process on the machine whose command line holds `hub.py`, `wait`
and `--pending`, and no `--for` other than `--for pm`, counts (`_wait_running` in
`kitlib/pmstart/__init__.py`): a session's `wait --pending --for <key>` waits on its own answers
and is not the PM's, but another PM session's plain `wait --pending` makes this line say running. Failures,
each one line:
- Nothing listens: `hub: hub server down (nothing listens on port <n>): start it with python
  "<kit>/tools/hub.py" serve`.
- No token file (neither the agent's nor the owner's can be read): `hub: no hub token file is there
  to read: run python "<kit>/tools/hub.py" setup`.
- `GET /api/v1/health` answered, but not ok: `hub: server did not answer as the hub does (health not ok)`.
- Any other failure of `GET /api/v1/health` (a timeout, a refusal, an answer that is not the hub's): `hub:
  server did not answer as the hub does (<exception class>)`; the class is all it names.
- `GET /api/v1/pending?for=pm` fails after `GET /api/v1/health` answered: `hub: server up on port <n>, pending not asked
  (<exception class>)`.
- The `hub` key is there and cannot be used (the machine config check refuses it, or the hub
  settings do): `hub: the hub key of the machine config cannot be used: <the configuration error's
  own text>`, on one line and cut at `HUB_ERROR_MAX` (400) characters. The text names the key and
  the rule it broke, never the rejected value (`_without_values` strips the `, not '<value>'`
  part); the `hub` key holds no token.
- A machine config that cannot be read at all, or whose check fails with no `hub` key in it: `hub:
  the machine config could not be read (<exception class>)`.
No token or server message reaches a line. The test home fence is not caught here
(`kitlib.config.user` `HomeFenced`). `pm_watch.py` counts as running when its
heartbeat (docs/pm-watch.md "Stream mode and heartbeat") is at most 5 minutes old
(`kitlib.pmstart` `BEAT_STALE`) and names the pid of a running `python` process whose command line
holds `pm_watch.py` and that started at the time the heartbeat records (within 2 seconds,
`START_SLACK`; skipped when either start time is unknown), so a reused pid does not count.
Otherwise the line says whether it stopped polling, is an older copy with no heartbeat, or is not
running, and gives the command to start it under Monitor. A running watcher whose heartbeat says it
does not stream gets a line to restart it with `--stream`, and a `pm_watch_cmd` without `--stream`
gets a line too: either watcher exits after one batch of events. Tests:
`tests/pmstart/test_pmstart.py` `WatcherTest`. The last check, always on and with no config key (after `console_tasks`, below), is the live foreground lock timeout (docs/windows-token-leak.md): a line while it is not 0, with the command the owner runs in their own PowerShell window. Code: `kitlib/pmstart/fglock.py`; tests: `tests/pmstart/test_fglock.py`.

## Kit currency (`kit_sync`)

When the machine config sets `pm_data` (docs/pm-data-branch.md), the `kit_sync` check keeps the
kit's main checkout current: it fetches `origin`'s `main` (`git fetch --no-tags`, explicit refspec, 3 second limit) and
fast-forwards local `main` ("kit updated to <short sha>"). It prints nothing when `main` already
contains `origin/main`. It leaves the checkout alone, with one "kit not updated: ..." line, when it
is not on `main`, has a merge, cherry-pick, revert or rebase in progress or uncommitted changes, when the landing lock is held, or
when the fetch fails (offline, timed out); a diverged `main` is reported, never merged. It changes
no git config and never rebases, resets or forces. The checks are a point in time (the landing lock is only read); `git merge --ff-only` bounds a race with a landing, since it refuses anything that is not a fast-forward. Code: `kitlib/pmstart` `kit_sync`; tests:
`tests/pmstart/test_kitsync.py`.

## Plugin export (`plugin_current`)

Right after `kit_sync`, the `plugin_current` check prints one line when the pm-kit plugin export is missing, behind the kit's `refs/remotes/<remote>/main`, or an account folder's last refresh failed, each with `python "<kit>/tools/plugin_refresh.py" --apply`, and when there is no export or an account folder is not migrated, with `python "<kit>/tools/plugin_refresh.py" --migrate --apply` instead (`<kit>` the kit's absolute path, `kitlib.pluginsource` `refresh_cmd`: the PM's session runs in another folder); an unreadable export state is a `could not check the pm-kit plugin` line. The refresh checks read the export root only (docs/plugin-source.md "Behind warnings"). The marketplace path check (`kitlib.pluginsource` `marketplace_mismatches`) also reads each migrated account folder's `plugins/known_marketplaces.json` `installLocation` and the machine's managed settings `extraKnownMarketplaces` source path, and prints one `pm-kit marketplace ... is <path>, not the export <root>` line per place that points elsewhere; an unreadable or wrong-shaped file is a `could not check the pm-kit marketplace path` line, a missing file or entry prints nothing, and it never writes or raises. Code: `kitlib/pmstart` `plugin_current`, `kitlib.pluginsource` `current_lines`; tests: `tests/pmstart/test_plugin_current.py`.

## Plugin enabled (`plugin_enabled`)

Right after `plugin_current`, the `plugin_enabled` check prints one line naming every account folder (`kitlib.pluginsource.claude.account_folders`) whose `settings.json` has no `enabledPlugins["pm-kit@pm-kit"]`: `pm-kit plugin: not enabled in <folders>: python "<kit>/tools/machine_setup.py" --apply (docs/machine-setup.md "Default mode")`. `false` is the owner's choice and prints nothing; a `settings.json` that is not a JSON object is its own `could not check the pm-kit plugin in <folder>` line; a machine config whose plugin settings cannot be read is the checklist's `could not check plugin_enabled` line. Code: `kitlib/pmstart` `plugin_enabled`; tests: `tests/pmstart/test_plugin_enabled_check.py`.

## Stubs (`stubs`)

For each repository in the machine config's `launcher.repos`, the `stubs` check runs
`kitlib.stubs.problem_line` and prints one line for a repository that uses stubs (its `.kit.json`
sets `"stubs".tools`, or it holds at least one) whose stubs are missing or stale; stubs are opt-in
(docs/install.md), so a repository with none prints nothing:
`stubs (<repo>): N missing/stale: <names>; run python <kit>/tools/kit_stubs.py write -C <folder>`,
with `(stale ones run the kit at <path>)` added when a stub runs another kit. A repository whose
stubs are current prints nothing; a folder that is not there, or a check that fails, is that
repository's own `could not check stubs (<repo>): ...` line. The check runs in the plugin's
export, so it compares each stub with the main checkout the export's marker names, the folder
`kit_stubs.py write` would name (`kitlib.stubs.live_kit`); an export whose marker names none gives
each repository that uses stubs `could not check stubs (<repo>): <export> is an export of the kit
...` with no write command, and a repository with none still nothing (docs/plugin-source.md
"Layout"). The coordinator's session start has the
same check for its own repository (docs/install.md "Tools without a stub"). Code: `kitlib/pmstart`
`stubs`; tests: `tests/pmstart/test_stubs_check.py`.

## Memory folder (`memory`)

After `stubs`, the `memory` check reads `.claude/settings.local.json`, then `.claude/settings.json`, of each repository in
`launcher.repos`, and of the PM repository (the main checkout holding `sessions_file`), through
`kitlib.stubs.memory.problem_line`. Files without `autoMemoryDirectory` give
`memory (<repo>): <file> has no autoMemoryDirectory; its memory folder is <folder>. Ask the owner
(via the hub) to run, in their own PowerShell window: python <kit>/tools/kit_stubs.py memory -C
<folder> --apply (it writes .claude/settings.local.json, which git ignores; nothing to commit)`: an owner step, since the command
writes another repository's main checkout. One it cannot read gives `could not check memory
(<repo>): ...`, as does a check that raises. A key naming another folder is the owner's choice and
prints nothing. The kit's folder is `<the main checkout's drive>/Agent-memory/<repo folder name>`.
Then `pm_start.memory_dir` must name the folder the PM repository's settings name (the owner's
choice when the key names another folder, else the kit's folder; case and slashes
aside), or one line says what to set it to. Claude Code ignores the key without a word when
`blockReadsOutsideWorkingDirectories` is set or the workspace is not trusted. Code:
`kitlib/pmstart` `memory`; tests: `tests/pmstart/test_pmstart.py` `MemoryTest`.

## Allow rules (`allow_rules`)

After `memory`, the `allow_rules` check reads the same two settings files of each repository in
`launcher.repos` and of the PM repository. It looks for permission rules (`allow`, `deny`, `ask`)
whose path names a folder under the plugin export root (`plugin.export_dir`, any case, either slash)
other than the current export. Each file and state gets one line:

`allow rules (<repo>): <file> names plugin export <folder> (gone|not current) in <n> rule(s); the
current export is <folder>: python "<kit>/tools/allow_rules.py" -C "<repo>" prints the land lines to paste; any other rule naming the old export needs its hash
changed by hand (owner)`

`allow_rules.py` prints only the land lines. The check flags any rule that names an old export, so
the owner edits the folder name in the others (a rule for another kit tool, say) by hand.

- `gone`: the folder holds no export marker.
- `not current`: an older export that is still there, so the allow runs an old `land.py`.
- A file it cannot read gives `could not check allow_rules (<repo>): <file>: ...`.
- A missing file prints nothing.
- With no repository, nothing is read.

The settings are the owner's, so the line names the command and writes nothing (docs/allow-rules.md).
Code: `kitlib/pmstart` `allow_rules`; tests: `tests/pmstart/test_allow_rules_check.py`.

## Repo prep (`repo_prep`)

After `allow_rules`, the `repo_prep` check runs the first-use repo prep (`kitlib.git.prep.run`; docs/new-worktree.md "First-use prep") on each repository in `launcher.repos`. The prep adds each host's worktree folder to the repository's `info/exclude` and sets the local `core.hooksPath` to the kit's `githooks/` when none is set and `.git/hooks` holds no hook. It prints a `repo prep: <folder>: ...` line only for what it did or could not do, so a repository already prepared prints nothing. A folder that is not there gives a `could not prep <repo>: ...` line. Code: `kitlib/pmstart` `repo_prep`; tests: `tests/pmstart/test_repo_prep.py`.

## Agent temp (`agent_temp`)

After `repo_prep`, the `agent_temp` check prints one line for each part of the agents' temp folder set-up
(docs/machine-setup.md "Extras") that is missing: "AGENT_TEMP is not set (machine_setup.py plans
it)" when neither the process nor the user environment has `AGENT_TEMP`; "agent_temp_sweep is not
set in the machine config (machine_setup.py plans it)"; and "agent temp sweep task agent-temp-sweep
is not registered (machine_setup.py --apply registers it, the owner's act)" when
`schtasks /Query` (4 second limit) says the task does not exist. A query that fails any other way
(access denied, the time limit, no `schtasks`) is "could not check the agent temp sweep task
agent-temp-sweep: ...", after the other two lines, never "not registered"
(`kitlib.machinesetup.agenttemp.task_registered`). It changes nothing. A failing environment or
config reader is one `could not check agent_temp: ...` line. Code: `kitlib/pmstart` `agent_temp_lines`; tests:
`tests/pmstart/test_agent_temp_check.py`.

## Console tasks (`console_tasks`)

After `agent_temp` and before the foreground lock check, always on and with no config key, the
`console_tasks` check reads every registered scheduled task's actions from
`schtasks /Query /XML ONE` (4 second limit) and prints one line for each action of a kit task whose
program is a console program (`python.exe`, `py.exe`, `powershell.exe`, `pwsh.exe`, `cmd.exe`,
`bash.exe`; `kitlib/pmstart/consoletasks.py` `CONSOLE`): "scheduled task <path> starts <program>, a
console program: a console window opens each time it runs. Its program should be pythonw.exe with
the carrier tools\no_window.py as its first argument (docs/daily-full.md "The schedule": ...)". Such
a task opens a window on the desktop each time it runs (docs/daily-full.md "The schedule"). A kit
task is one with a name the kit registers (the launcher's sign-in task, `agent-temp-sweep`, a name
ending `-worktree-sweep` or `-daily-full`) or an action with a word (its program, or an argument,
quoted or not, with a leading `&`, quotes and an option's `-opt:` / `--opt=` taken off, `words`)
that is an absolute path inside a kit folder (`is_kit`, `kit_roots`): this kit; the
live checkout an export runs for (`KIT_LIVE_ROOT`, else the main checkout the export's marker
records when that holds `kit.json` and a `tools` folder); and, when this kit is a plugin export,
the folder that holds every export. A file named like a kit tool in another folder, another
repository's `<repo>\tools\gate.py`, is not the kit's. A task that names the kit's checkout while
the session start runs from a plugin export whose marker names no usable checkout is matched by
its name only. A `pythonw.exe` program
is never flagged, with or without the carrier. `schtasks` writes bytes: UTF-16 output (a byte order
mark, or NULs) goes to the XML parser as bytes, which reads the declaration itself; single-byte
output is read in the OEM code page, its declaration dropped unless it names a single-byte encoding,
since `schtasks /Query /XML` declares `encoding="UTF-16"` over single-byte text on a pipe and
`/XML ONE` declares none (`xml_input`). It changes nothing; a failed read (access denied, the time
limit, no `schtasks`) is one `could not check console_tasks: ...` line. Code:
`kitlib/pmstart/consoletasks.py`; tests: `tests/pmstart/test_console_tasks.py`.

## Hand-off file

The PM keeps one hand-off file in the PM repository and edits it in place, never a dated copy
(`kitlib.pmstart` `handoff`; tests `HandoffTest` in `tests/pmstart/test_pmstart.py`). The check reads
only the file `pm_start.handoff_file` names and prints `read first: <file>`, or `no hand-off file
<file>: create it ...` when it is not there. When `pm_start.memory_dir` is set and holds any
`pm-handoff*.md`, one more line says `hand-off notes belong in <file>: merge and delete <those files>`.
A relative `handoff_file` with no PM repository found (no readable `sessions_file`) is one `could not
check handoff` line. The former `handoff_glob` key is not read. The PM repository git-ignores
`HANDOFF.md` (an owner decision): it is a working note and is never committed.

## Config

The machine config (docs/kit-json.md "Machine config keys"), key `pm_start`; without it the checklist says it is skipped.
`<home>`, `<pm-repo>` and `<kit>` stand for absolute paths.

```json
{"pm_start": {
  "pm_watch_cmd": "python -u <kit>/tools/pm_watch.py --stream",
  "memory_dir": "<repo drive>/Agent-memory/<pm-repo>",
  "handoff_file": "HANDOFF.md",
  "reminders_file": "<config-dir>/reminders.json",
  "sessions_file": "<launcher state_dir>/sessions.json",
  "eng_review": ["EX-API-*"]
}}
```

- `handoff_file` (optional): the PM's one hand-off file, default `HANDOFF.md` in the PM repository's
  main checkout (the repository holding `sessions_file`, `kitlib.pmstart` `pm_memory_repo`); an absolute
  path is used as is. See "Hand-off file".
- `reminders_file`: `[{"due": "YYYY-MM-DD", "text": "..."}]`.
- `sessions_file`: `{"<key>": {"name": "<remote-control name>", "repo": "<repo>"}}`; an entry
  with `autostart: false` is not checked.
- `pm_watch_cmd`: the command the watcher line prints, as written (docs/pm-watch.md); without
  `--stream` the checklist adds a line saying so. Unset, it is
  the kit's own `python -u "<kit>/tools/pm_watch.py" --stream` (`kitlib.pmstart`
  `PM_WATCH_DEFAULT_CMD`).
- `eng_review` (optional, list of strings): the owner's design-review flags
  (docs/engineering-rulings.md), printed each start so the PM sends matching ENG questions (from
  a consultant or a coordinator) to the hub. Empty or absent prints nothing; a value that is not
  a list of non-empty strings prints "could not check eng_review".
- Optional too: `standing` (strings, each printed as "standing rule:"), `repos` (more repo names
  for the hub's menus) and `repo_paths` ({repo: folder} for the hub's backlog view,
  `kitlib.hubbacklog` `repo_root`; default the PM repository's sibling folder).

## Hook (settings.json)

Only in settings the PM session loads; the hook does not check which session runs it. It is not
in the plugin, which every session loads. Exec form, with `<pythonw>` the interpreter that
`boot/hook.py claude --settings-entry` prints (docs/guard.md "Go-live"), starts no shell. `pythonw` has no
console, so each program the checklist starts is started with a console that has no window: git
(`kitlib.git.cmd`) and the PowerShell that lists processes (`kitlib/pmstart` `list_processes`;
`tests/pmstart/test_plumbing.py`) through `kitlib.proc.spawn`, `schtasks` with `CREATE_NO_WINDOW`
in `kitlib.machinesetup.agenttemp.task_registered` (docs/daily-full.md "The schedule"). Put it in the PM
repo's `.claude/settings.local.json`, which only that checkout loads. `tools/machine_setup.py`
otherwise reads the account config folders (`~/.claude`, `~/.claude-*`, `default_config_dirs` in
`kitlib/machinesetup`), never a repository's settings, so this file is edited by hand, except that
`machine-setup.ps1 -PmSettings <file>` (its default mode) writes the hook to the one file it names and
refuses an account's `settings.json` (docs/machine-setup.md "Default mode").

```json
{"hooks":{"SessionStart":[{"matcher":"startup|resume|compact","hooks":[{"type":"command","command":"<pythonw>","args":["<kit>/boot/hook.py","claude","SessionStart","pm"]}]}]}}
```
