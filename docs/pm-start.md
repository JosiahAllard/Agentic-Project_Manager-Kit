# PM session-start checklist

`claude/hooks/pm_session_start.py` prints a checklist at every PM session start (as
`additionalContext`; logic `kitlib/pmstart`). A hook cannot call tools, so it lists steps for the
PM. It always exits 0; a failed check becomes one "could not check X: reason" line.

Checks: standing rules, eng_review flags, the hub (republish it to arm the alert), `pm_watch.py`
running, the newest handoff file, reminders due, and coordinators not running (no `claude.exe`
with the exact, case-sensitive `--remote-control` name, as `kitlib/launcher/lookup.py`); each such line names the
`launch.py start --session <key>` that starts it (docs/launcher.md).

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

`~/.claude-pm-kit/config.json`, key `pm_start`; without it the checklist says it is skipped.
`<home>`, `<pm-repo>` and `<kit>` stand for absolute paths.

```json
{"pm_start": {
  "hub_url": "https://claude.ai/artifact/<id>",
  "hub_source": "claude/hub/action-hub.html",
  "pm_watch_cmd": "python -u <kit>/tools/pm_watch.py",
  "memory_dir": "<home>/.claude/projects/<project>/memory",
  "handoff_glob": "pm-handoff-*.md",
  "reminders_file": "<home>/.claude-pm-kit/reminders.json",
  "sessions_file": "<launcher state_dir>/sessions.json",
  "eng_review": ["SCRIPT-API-*"]
}}
```

- `reminders_file`: `[{"due": "YYYY-MM-DD", "text": "..."}]`.
- `sessions_file`: `{"<key>": {"name": "<remote-control name>", "repo": "<repo>"}}`; an entry
  with `autostart: false` is not checked.
- `pm_watch_cmd`: starts the watcher (docs/pm-watch.md).
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
