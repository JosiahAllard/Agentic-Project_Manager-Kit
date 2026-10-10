# PM watcher

`tools/pm_watch.py` stays silent until something needs the PM, prints one line per event and exits; the PM runs it in the background and relaunches it after each event. Logic: `kitlib/pmwatch` (checks and the `Watcher` poll), with the Claude-only parts (`~/.claude*/projects` transcripts, the `custom-title` record, the `~/.claude/machine-lock/alerts` default) in `claude/claude_host.py`, the seam the host split (ARCHITECTURE "Host split") lifts out. Ported from the PM repo's `launcher/pm_watch.py` at 95f7262: the event lines, state keys (`branches`, `usage`, `bg_alerts`, `idle`, `idle_nolog`) and options are the same; the tool's docstring lists the events.

## Config

The machine config (docs/kit-json.md "Machine config keys"), key `pm_watch`; a flag of the same meaning wins:

```json
{"pm_watch": {
  "sessions_file": "<launcher state_dir>/sessions.json",
  "state_file": "<local-app-data>/pm_watch/state.json",
  "alerts_dir": "<home>/.claude/machine-lock/alerts"
}}
```

`<home>`, `<local-app-data>`, `<pm-repo>` and `<kit>` stand for absolute paths: the user's home, `%LOCALAPPDATA%`, the PM repository and this kit's checkout.

`sessions_file` (`--sessions`) is required: neither flag nor key exits 2. `usage_script` (`--usage-script`) is optional: a PowerShell script whose output is read as the usage lines; unset, the usage is read in-process from `kitlib.launcher.usage` (`launch.py usage`, docs/launcher.md "Usage") with the machine config's `launcher` settings; with neither a script nor a `launcher` key it exits 2 (`kitlib.pmwatch.launcher_configured`), since the default would watch only `~/.claude`. `state_file` (`--state`) defaults to `%LOCALAPPDATA%\pm_watch\state.json`, the file the PM's copy uses; `alerts_dir` (`--alerts-dir`) defaults to `~/.claude/machine-lock/alerts`.

Kit-only additions: `--state`, and `--once` (one poll, print any events, exit 0) for checks.

## Switch-over for the PM repo (at go-live)

The PM repository keeps no watcher code. Set `pm_watch.sessions_file` to the launcher's sessions.json and point `pm_start.pm_watch_cmd` at `python -u <kit>/tools/pm_watch.py` (docs/pm-start.md "Config"); `kitlib/pmstart` finds it as a `pm_watch.py` process. With no `usage_script`, usage is read in-process, so `launcher/usage.ps1` is not needed. Stop the old watcher, start the kit one, then delete `launcher/pm_watch.py` and `launcher/test_pm_watch.py` (the tests are in `tests/pmwatch`); the state file default is the same, so no event repeats.
