# PM session-start checklist

`claude/hooks/pm_session_start.py` prints a checklist at every PM session start (as
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
holds `pm_watch.py`. Otherwise the line says whether it stopped polling, is an older copy with no
heartbeat, or is not running, and gives the command to start it under Monitor. Tests:
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

## Config

The machine config (docs/kit-json.md "Machine config keys"), key `pm_start`; without it the checklist says it is skipped.
`<home>`, `<pm-repo>` and `<kit>` stand for absolute paths.

```json
{"pm_start": {
  "hub_urls": {"1": "https://claude.ai/artifact/<id>"},
  "hub_source": "claude/hub/action-hub.html",
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
  (default `claude/hub/action-hub.html`); the hub lines name it as an absolute path, and a file
  that does not exist replaces the publish step with a line saying so. `hub_mirror`: the absolute
  mirror folder for an install without `pm_data`. Code: `kitlib/pmstart` `hub`, `hub_source`;
  tests: `tests/pmstart/test_hub_accounts.py`, `tests/pmstart/test_hub_line.py`.
- `reminders_file`: `[{"due": "YYYY-MM-DD", "text": "..."}]`.
- `sessions_file`: `{"<key>": {"name": "<remote-control name>", "repo": "<repo>"}}`; an entry
  with `autostart: false` is not checked.
- `pm_watch_cmd`: the command the watcher line prints, as written (docs/pm-watch.md). Unset, it is
  the kit's own `python -u "<kit>/tools/pm_watch.py" --stream` (`kitlib.pmstart`
  `PM_WATCH_DEFAULT_CMD`).
- `eng_review` (optional, list of strings): the owner's design-review flags
  (docs/engineering-rulings.md), printed each start so the PM sends matching ENG questions (from
  a consultant or a coordinator) to the hub. Empty or absent prints nothing; a value that is not
  a list of non-empty strings prints "could not check eng_review".
- Optional too: `standing` (strings, each printed as "standing rule:"), `repos` (more repo names
  for the hub's menus) and `repo_paths` ({repo: folder} for `claude/hub/hub_backlog.py`; default
  the PM repository's sibling folder).

## Hook (settings.json)

Only in settings the PM session loads; the hook does not check which session runs it.

```json
{"hooks":{"SessionStart":[{"matcher":"startup|resume|compact","hooks":[{"type":"command","command":"python <kit>/claude/hooks/pm_session_start.py"}]}]}}
```
