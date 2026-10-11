# PM watcher

`tools/pm_watch.py` stays silent until something needs the PM and prints one line per event. With `--stream` it keeps polling after an event, so the PM runs it once under Monitor and each batch of events arrives as a notification. Without it, it exits after the first events and the PM relaunches it in the background after each one. Logic: `kitlib/pmwatch` (checks and the `Watcher` poll), with the Claude-only parts (`~/.claude*/projects` transcripts, the `custom-title` record, the `~/.claude/machine-lock/alerts` default) in `claude/claude_host.py`, the seam the host split (ARCHITECTURE "Host split") lifts out. Ported from the PM repo's `launcher/pm_watch.py` at 95f7262: the event lines, state keys (`branches`, `usage`, `bg_alerts`, `idle`, `idle_nolog`) and options are the same; the tool's docstring lists the events.

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

`sessions_file` (`--sessions`) is required: neither flag nor key exits 2. Usage is read in-process by `kitlib.pmwatch.usage` from `kitlib.launcher.usage` (`launch.py usage`, docs/launcher.md "Usage") with the machine config's `launcher` settings; without a `launcher` key it exits 2 (`kitlib.pmwatch.launcher_configured`), since the default would watch only `~/.claude`. The flag `--usage-script` and the key `pm_watch.usage_script` are refused with exit 2. `state_file` (`--state`) defaults to `%LOCALAPPDATA%\pm_watch\state.json`; `alerts_dir` (`--alerts-dir`) defaults to `~/.claude/machine-lock/alerts`.

Kit-only additions: `--state`, `--stream`, the heartbeat below, and `--once` (one poll, print any events, exit 0) for checks. `--stream` and `--once` exclude each other (exit 2).

## Stream mode and heartbeat

Every pass except a `--once` run writes `<state file>.beat`, holding `{"pid", "beat", "stream", "started"}`: the watcher's process id, the time of the pass in epoch seconds, whether it streams, and when the process started in epoch seconds (`kitlib.pmwatch` `own_start_time`, null when Windows does not report it), so a reused pid is not taken for the watcher. The heartbeat is a separate file because the state file's keys are shared with the PM's copy. A heartbeat write that fails (an `OSError`, such as a `PermissionError` while another process holds the file) is skipped and tried again on the next pass, so it never stops the watcher; the heartbeat then goes stale. A pass sleeps 30 seconds (`kitlib.pmwatch` `POLL_SLEEP`). The PM session-start check reads the heartbeat beside the configured `state_file` (docs/pm-start.md): a heartbeat more than 5 minutes old, one whose pid is not a running `pm_watch.py` that started at the recorded time, or one that says it does not stream, gives a line to start the watcher again. Code: `kitlib.pmwatch` `run`, `write_heartbeat`, `read_heartbeat`, `own_start_time`; tests: `tests/pmwatch/test_stream.py`.

## The PM repo

The PM repository keeps no watcher code. It sets `pm_watch.sessions_file` to the launcher's sessions.json and points `pm_start.pm_watch_cmd` at `python -u <kit>/tools/pm_watch.py --stream` (docs/pm-start.md "Config"), or leaves it unset for the kit's own command; `kitlib/pmstart` finds it as a `pm_watch.py` process whose heartbeat is fresh.
