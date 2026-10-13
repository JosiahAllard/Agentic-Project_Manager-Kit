# PM session-start checklist

`hosts/claude/shell/hooks/pm_session_start.py` prints a checklist at every PM session start (as
`additionalContext`; logic `kitlib/pmstart`). A hook cannot call tools, so it lists steps for the
PM. It always exits 0; a failed check becomes one "could not check X: reason" line.

Checks: standing rules, eng_review flags, the hub, `pm_watch.py` running, the newest handoff file, reminders due, and coordinators not running (no `claude.exe`
with the exact, case-sensitive `--remote-control` name, as `kitlib/launcher/lookup.py`); each such line names the
`launch.py start --session <key> --account <n>` that starts it (docs/launcher.md). `<n>` is the account
this PM session runs on (`kitlib.pmstart` `current_account`), not the entry's stored one, so the PM
launches every coordinator, the kit's included, from whatever account it started on; when that
account cannot be identified the line says `--account auto` (pick-account's choice). Tests:
`tests/pmstart/test_pmstart.py` `LivenessTest`. The hub lines are steps, since the hook sees
neither the PM's ArtifactComments subscription nor the hub's data: republish the page (or publish a
copy), start the ArtifactComments watch on it (again after a resume or compact), check that its
`backlog` collection is filled (empty means a new copy: refill the regenerated collections), then
process the hub. Tests: `tests/pmstart/test_hub_line.py`. `pm_watch.py` counts as running when its
heartbeat (docs/pm-watch.md "Stream mode and heartbeat") is at most 5 minutes old
(`kitlib.pmstart` `BEAT_STALE`) and names the pid of a running `python` process whose command line
holds `pm_watch.py` and that started at the time the heartbeat records (within 2 seconds,
`START_SLACK`; skipped when either start time is unknown), so a reused pid does not count.
Otherwise the line says whether it stopped polling, is an older copy with no heartbeat, or is not
running, and gives the command to start it under Monitor. A running watcher whose heartbeat says it
does not stream gets a line to restart it with `--stream`, and a `pm_watch_cmd` without `--stream`
gets a line too: either watcher exits after one batch of events. Tests:
`tests/pmstart/test_pmstart.py` `WatcherTest`. The last check, always on and with no config key, is the live foreground lock timeout (docs/windows-token-leak.md): a line while it is not 0, with the command the owner runs in their own PowerShell window. Code: `kitlib/pmstart/fglock.py`; tests: `tests/pmstart/test_fglock.py`.

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

Right after `kit_sync`, the `plugin_current` check prints one line when the pm-kit plugin export is missing, behind the kit's `refs/remotes/<remote>/main`, or an account folder's last refresh failed, each with `python tools/plugin_refresh.py --apply`, and when there is no export or an account folder is not migrated, with `python tools/plugin_refresh.py --migrate --apply` instead; an unreadable export state is a `could not check the pm-kit plugin` line. It reads the export root only, never an account folder (docs/plugin-source.md "Behind warnings"). Code: `kitlib/pmstart` `plugin_current`, `kitlib.pluginsource` `current_lines`; tests: `tests/pmstart/test_plugin_current.py`.

## Stubs (`stubs`)

For each repository in the machine config's `launcher.repos`, the `stubs` check runs
`kitlib.stubs.problem_line` and prints one line for a repository whose stubs are missing or stale:
`stubs (<repo>): N missing/stale: <names>; run python <kit>/tools/kit_stubs.py write -C <folder>`,
with `(stale ones run the kit at <path>)` added when a stub runs another kit. A repository whose
stubs are current prints nothing; a folder that is not there, or a check that fails, is that
repository's own `could not check stubs (<repo>): ...` line. The coordinator's session start has the
same check for its own repository (docs/install.md "Tools without a stub"). Code: `kitlib/pmstart`
`stubs`; tests: `tests/pmstart/test_stubs_check.py`.

## Agent temp (`agent_temp`)

After `stubs`, the `agent_temp` check prints one line for each part of the agents' temp folder set-up
(docs/machine-setup.md "Extras") that is missing: "AGENT_TEMP is not set (machine_setup.py plans
it)" when neither the process nor the user environment has `AGENT_TEMP`; "agent_temp_sweep is not
set in the machine config (machine_setup.py plans it)"; and "agent temp sweep task agent-temp-sweep
is not registered (machine_setup.py --register-sweep-task --apply, the owner's act)" when
`schtasks /Query` (4 second limit) says the task does not exist. A query that fails any other way
(access denied, the time limit, no `schtasks`) is "could not check the agent temp sweep task
agent-temp-sweep: ...", after the other two lines, never "not registered"
(`kitlib.machinesetup.agenttemp.task_registered`). It changes nothing. A failing environment or
config reader is one `could not check agent_temp: ...` line. Code: `kitlib/pmstart` `agent_temp_lines`; tests:
`tests/pmstart/test_agent_temp_check.py`.

## Config

The machine config (docs/kit-json.md "Machine config keys"), key `pm_start`; without it the checklist says it is skipped.
`<home>`, `<pm-repo>` and `<kit>` stand for absolute paths.

```json
{"pm_start": {
  "hub_urls": {"1": "https://claude.ai/artifact/<id>"},
  "hub_source": "hosts/claude/shell/hub/action-hub.html",
  "pm_watch_cmd": "python -u <kit>/tools/pm_watch.py --stream",
  "memory_dir": "<home>/.claude/projects/<project>/memory",
  "handoff_glob": "pm-handoff-*.md",
  "reminders_file": "<config-dir>/reminders.json",
  "sessions_file": "<launcher state_dir>/sessions.json",
  "eng_review": ["EX-API-*"]
}}
```

- `hub_urls`: account label -> that account's hub copy (docs/action-hub.md "One copy per
  account"; `tools/hub_mirror.py set-url` writes it). This account's URL gives the republish
  line; none gives the steps to publish a copy, record it and restore it from the mirror.
  `hub_url` (one URL) is read only while `hub_urls` has no entry; when both are set the checklist
  says `hub_url` is ignored. `hub_source`: the page to publish, absolute or relative to the kit
  (default `hosts/claude/shell/hub/action-hub.html`); the hub lines name it as an absolute path, and a file
  that does not exist, or a value that is not a string, replaces the publish step with a line
  saying so; the watch and process lines still print. `hub_mirror`: the absolute
  mirror folder for an install without `pm_data`. Code: `kitlib/pmstart` `hub`, `hub_source`;
  tests: `tests/pmstart/test_hub_accounts.py`, `tests/pmstart/test_hub_line.py`.
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
  for the hub's menus) and `repo_paths` ({repo: folder} for `hosts/claude/shell/hub/hub_backlog.py`; default
  the PM repository's sibling folder).

## Hook (settings.json)

Only in settings the PM session loads; the hook does not check which session runs it. It is not
in the plugin, which every session loads. Exec form, with `<pythonw>` the interpreter that
`guard.py --settings-entry` prints (docs/guard.md "Go-live"), starts no shell. `pythonw` has no
console, so each program the checklist starts is started with a console that has no window: git
(`kitlib.git.cmd`) and the PowerShell that lists processes (`kitlib/pmstart` `list_processes`;
`tests/pmstart/test_plumbing.py`) through `kitlib.proc.spawn`, `schtasks` with `CREATE_NO_WINDOW`
in `kitlib.machinesetup.agenttemp.task_registered` (docs/daily-full.md "The schedule"). Put it in the PM
repo's `.claude/settings.local.json`, which only that checkout loads. `tools/machine_setup.py`
otherwise reads the account config folders (`~/.claude`, `~/.claude-*`, `default_config_dirs` in
`kitlib/machinesetup`), never a repository's settings, so this file is edited by hand, except that
`machine-setup.ps1 -Fresh -PmSettings <file>` writes the hook to the one file it names and refuses an
account's `settings.json` (docs/machine-setup.md "Fresh machine (--fresh)").

```json
{"hooks":{"SessionStart":[{"matcher":"startup|resume|compact","hooks":[{"type":"command","command":"<pythonw>","args":["<kit>/hosts/claude/shell/hooks/pm_session_start.py"]}]}]}}
```
