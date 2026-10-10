# PM session-start checklist

`claude/hooks/pm_session_start.py` prints a checklist at every PM session start (as
`additionalContext`; logic `kitlib/pmstart`). A hook cannot call tools, so it lists steps for the
PM. It always exits 0; a failed check becomes one "could not check X: reason" line.

Checks: standing rules, eng_review flags, the hub (republish it to arm the alert), `pm_watch.py`
running, the newest handoff file, reminders due, and coordinators not running (no `claude.exe`
with the exact, case-sensitive `--remote-control` name, as `kitlib/launcher/lookup.py`); each such line names the
`launch.py start --session <key>` that starts it (docs/launcher.md).

## Config

`~/.claude-pm-kit/config.json`, key `pm_start`; without it the checklist says it is skipped.
`<home>`, `<pm-repo>` and `<kit>` stand for absolute paths.

```json
{"pm_start": {
  "hub_url": "https://claude.ai/artifact/<id>",
  "hub_source": "claude/hub/action-hub.html",
  "pm_watch_cmd": "python launcher/pm_watch.py",
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
