# PM session-start checklist

`boot/hook.py claude SessionStart pm` (the kit's one hook entry, its `pm` part) prints a checklist at every PM session start (as
`additionalContext`; logic `kitlib/pmstart`). A hook cannot call tools, so it lists steps for the
PM. It always exits 0; a failed check becomes one "could not check X: reason" line.

Checks: standing rules, eng_review flags, the hub, `pm_watch.py` running, the newest handoff file, reminders due, and coordinators not running (no `claude.exe`
with the exact, case-sensitive `--remote-control` name, as `kitlib/launcher/lookup.py`); each such line names the
`launch.py start --session <key> --account <n>` that starts it (docs/launcher.md). `<n>` is the account
this PM session runs on (`kitlib.pmstart` `current_account`), not the entry's stored one, so the PM
launches every coordinator, the kit's included, from whatever account it started on; when that
account cannot be identified the line says `--account auto` (pick-account's choice).

Hub (`kitlib.pmstart` `hub_site`; docs/action-hub.md): the lines read the machine config's `hub`
key, or every default (port 8790). The check only asks the loopback server (`GET /api/v1/health`,
then `GET /api/v1/pending?for=pm`, 1.5 seconds each) and never starts it. The lines:
`hub: server up on port <n>, <k> pending for the PM`; when `<k>` is above 0, a line to process the
items (`hub.py pending --for pm`); then either `hub: wait --pending is running` or a warning telling
the PM to start `hub.py wait --pending --for pm` in the background (docs/action-hub.md "Who handles
a pending item"). "Running" is matched by command text only. A failure is one line naming the fix:
the server down (`hub.py serve`), no token file (`hub.py setup`), an answer that is not the hub's, a
`hub` key that cannot be used (naming the key and the rule, never the value), or a machine config
that cannot be read. No token or server message reaches a line.

`pm_watch.py` counts as running when its heartbeat (docs/pm-watch.md "Stream mode and heartbeat")
is at most 5 minutes old and names a running `pm_watch.py` process that started at the recorded
time. Otherwise the line says whether it stopped polling, has no heartbeat, or is not running, and
gives the command to start it under Monitor; a watcher or `pm_watch_cmd` without `--stream` gets a
line too. The last check, always on and with no config key, is the live foreground lock timeout
(docs/windows-token-leak.md): a line while it is not 0, with the command the owner runs in their
own PowerShell window (`kitlib/pmstart/fglock.py`).

## Kit currency (`kit_sync`)

When the machine config sets `pm_data` (docs/pm-data-branch.md), the `kit_sync` check keeps the
kit's main checkout current: it fetches `origin`'s `main` (`git fetch --no-tags`, explicit refspec, 3 second limit) and
fast-forwards local `main` ("kit updated to <short sha>"). It prints nothing when `main` already
contains `origin/main`. It leaves the checkout alone, with one "kit not updated: ..." line, when it
is not on `main`, has a merge, cherry-pick, revert or rebase in progress or uncommitted changes, when the landing lock is held, or
when the fetch fails (offline, timed out); a diverged `main` is reported, never merged. It changes
no git config and never rebases, resets or forces.

## Plugin export (`plugin_current`)

Right after `kit_sync`, the `plugin_current` check prints one line when the pm-kit plugin export is missing, behind the kit's `refs/remotes/<remote>/main`, or an account folder's last refresh failed, each with `python "<kit>/tools/plugin_refresh.py" --apply`, and when there is no export or an account folder is not migrated, with `python "<kit>/tools/plugin_refresh.py" --migrate --apply` instead; an unreadable export state is a `could not check the pm-kit plugin` line. It reads the export root only, never an account folder (docs/plugin-source.md "Behind warnings").

## Stubs (`stubs`)

For each repository in the machine config's `launcher.repos`, the `stubs` check runs
`kitlib.stubs.problem_line` and prints one line for a repository that uses stubs (its `.kit.json`
sets `"stubs".tools`, or it holds at least one) whose stubs are missing or stale; stubs are opt-in
(docs/install.md), so a repository with none prints nothing:
`stubs (<repo>): N missing/stale: <names>; run python <kit>/tools/kit_stubs.py write -C <folder>`,
with `(stale ones run the kit at <path>)` added when a stub runs another kit. A repository whose
stubs are current prints nothing; a folder that is not there, or a check that fails, is that
repository's own `could not check stubs (<repo>): ...` line. The check compares each stub with the main checkout the plugin export's marker names
(docs/plugin-source.md "Layout"). The coordinator's session start has the
same check for its own repository (docs/install.md "Tools without a stub").

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
aside), or one line says what to set it to.

## Repo prep (`repo_prep`)

After `memory`, the `repo_prep` check runs the first-use repo prep (`kitlib.git.prep.run`; docs/new-worktree.md "First-use prep") on each repository in `launcher.repos`. The prep adds each host's worktree folder to the repository's `info/exclude` and sets the local `core.hooksPath` to the kit's `githooks/` when none is set and `.git/hooks` holds no hook. It prints a `repo prep: <folder>: ...` line only for what it did or could not do, so a repository already prepared prints nothing. A folder that is not there gives a `could not prep <repo>: ...` line.

## Agent temp (`agent_temp`)

After `memory`, the `agent_temp` check prints one line for each part of the agents' temp folder set-up
(docs/machine-setup.md "Extras") that is missing: "AGENT_TEMP is not set (machine_setup.py plans
it)" when neither the process nor the user environment has `AGENT_TEMP`; "agent_temp_sweep is not
set in the machine config (machine_setup.py plans it)"; and "agent temp sweep task agent-temp-sweep
is not registered (machine_setup.py --apply registers it, the owner's act)" when
`schtasks /Query` (4 second limit) says the task does not exist. A query that fails any other way is "could not check the agent temp sweep task
agent-temp-sweep: ...", never "not registered". It changes nothing. A failing environment or
config reader is one `could not check agent_temp: ...` line.

## Console tasks (`console_tasks`)

After `agent_temp` and before the foreground lock check, always on and with no config key, the
`console_tasks` check reads every registered scheduled task's actions (`schtasks /Query /XML ONE`,
4 second limit) and prints one line for each action of a kit task whose program is a console
program (`python.exe`, `powershell.exe`, `cmd.exe` and the like): such a task opens a window on the
desktop each time it runs, and its program should be `pythonw.exe` with the carrier
`tools\no_window.py` (docs/daily-full.md "The schedule"). A kit task is one with a name the kit
registers or an action naming a path inside a kit folder. It changes nothing; a failed read is one
`could not check console_tasks: ...` line (`kitlib/pmstart/consoletasks.py`).

## Config

The machine config (docs/kit-json.md "Machine config keys"), key `pm_start`; without it the checklist says it is skipped.
`<home>`, `<pm-repo>` and `<kit>` stand for absolute paths.

```json
{"pm_start": {
  "pm_watch_cmd": "python -u <kit>/tools/pm_watch.py --stream",
  "memory_dir": "<repo drive>/Agent-memory/<pm-repo>",
  "handoff_glob": "pm-handoff-*.md",
  "reminders_file": "<config-dir>/reminders.json",
  "sessions_file": "<launcher state_dir>/sessions.json",
  "eng_review": ["EX-API-*"]
}}
```

- `reminders_file`: `[{"due": "YYYY-MM-DD", "text": "..."}]`.
- `sessions_file`: `{"<key>": {"name": "<remote-control name>", "repo": "<repo>"}}`; an entry
  with `autostart: false` is not checked.
- `pm_watch_cmd`: the command the watcher line prints, as written (docs/pm-watch.md); without
  `--stream` the checklist adds a line saying so. Unset, it is
  the kit's own `python -u "<kit>/tools/pm_watch.py" --stream`.
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
`boot/hook.py claude --settings-entry` prints (docs/guard.md "Go-live"), starts no shell. Put it in the PM
repo's `.claude/settings.local.json`, which only that checkout loads. It is edited by hand, or `machine-setup.ps1 -PmSettings <file>` writes it (docs/machine-setup.md "Default mode").

```json
{"hooks":{"SessionStart":[{"matcher":"startup|resume|compact","hooks":[{"type":"command","command":"<pythonw>","args":["<kit>/boot/hook.py","claude","SessionStart","pm"]}]}]}}
```
