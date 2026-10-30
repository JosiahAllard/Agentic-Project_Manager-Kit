# coord_state.py

A coordinator's event log and the state note rendered from it: `tools/coord_state.py` (entry) over `kitlib/coordstate` (`log.py`, the log; `render.py`, the note). It is opt-in per session and is a trial: with no `coord_state.py start`, nothing here runs and a coordinator's state note is the hand-written one the role describes (content/roles/coordinator.md).

What it solves: a coordinator's note is written by hand, so what a tool already knows (a landing submitted, a landing that ended, a feed post that failed) is lost when the note was not updated before a compaction. With the log on, tools record those facts and `show` renders them. The coordinator writes only what no tool can see, its judgments.

## Files

All in the coordinator folder, `<agent temp>/<repo slug>-coordinator/`, outside the repository (agent temp is `AGENT_TEMP`, else `TEMP`, else the home folder). `<session>` is `PM_KIT_SESSION_NAME` (`kitlib.sessionmark.SESSION_NAME_ENV`), which the kit launcher sets and every tool shell, subagent and detached landing of the session inherits. A name that is not 1 to 100 letters, digits, `.`, `_` or `-` (starting with a letter or digit) is no session.

| File | Who writes it | What it is |
|---|---|---|
| `events-<session>.jsonl` | the tools | the event log; its existing is the switch that turns everything on |
| `events-<session>.jsonl.off` | `stop` | the log turned off, kept for `start` to put back |
| `.events-<session>.lock` | the tools | the OS lock appends and compactions hold (`kitlib.fs.treelock`) |
| `judgments-<session>.md` | the coordinator | what no tool can see: the only file it edits |
| `state-<session>.md` | `show` | the rendered note, at the path the start context, the hand-off and the launcher already read |

## The event log

One JSON object per line: `{"v":1,"kind":...,"utc":...,<fields>}`, a field that is null left out, fields sorted.

| `kind` | Fields | Said by |
|---|---|---|
| `land_submit` | `branches`, `tips`, `reviewed_by`, `output` (the submission's `.land.log`) | `land.py --submit`, `gate.py land --submit`, once a submission was taken (exit 0) |
| `backlog` | `verb`, `id`, `branch` | `backlog.py`, after a write it published (the same place as its audit line); `id` and `branch` when the verb has them |
| `reported` | `branch`, `tip` | `coord_state.py reported` |

Limits, in `kitlib/coordstate/log.py`:

- **The log is never created by an append.** An append to a log that is gone fails, so a `stop` cannot be undone by a tool that raced it.
- **Concurrent tools never interleave.** An append holds the lock and writes its line in one write. A line a crash cut short is skipped on read and counted in the note's header; the next append starts a new line, so only the cut line is lost.
- **A busy lock costs at most `BOUND` seconds** (0.72): tried for `WAIT` (0.2 s), plus up to 0.5 s the lock takes to read its holder's record. Then that event is dropped.
- **A full log drops events.** At `MAX_BYTES` (512 KB) it takes no more lines until `show` compacts it. `show` compacts past `COMPACT_AT` (128 KB) to the events the note shows, keeping every line appended since its read; it says so in a `COMPACTED` line on stderr. A compaction that cannot run (a busy lock, a failed write) is a `NOT COMPACTED` line there and changes nothing else: the note is still written and printed, and the next `show` tries again.

## Who appends

A tool's call (`log.note`, `note_submit`, `note_backlog`) is best effort: no session, no log, a busy lock, a full log or a failed write means no line, and it never raises or changes what the tool does, says or exits with. With no log on, a call costs one environment read and one folder listing.

A subagent inherits the session's name, so its `backlog.py` write or submitted landing is logged like the coordinator's own. Nothing appends from the detached landing, a hook or the verdict queue: `show` reads what those record (The state note). Verdicts are read from the queue each time, never appended. Gate results are not in the log or the note in this slice: they come in slice 2, with the dispatch event.

A landing not submitted (`land.py` without `--submit`) has no `land_submit` event, so it is not in the note, and `reported` on its branch says `NOT FOUND`.

## The state note

`show` renders the note from the log, the judgments file and what the repository already records, held to 8 KB (`render.CAP`) by dropping the oldest lines of the least needed sections: backlog writes first, then reported landings, then landings on the feed. Judgments are never trimmed, so judgments alone can pass the cap. In order:

1. **Judgments**: `judgments-<session>.md`, verbatim.
2. **Landings to report to the PM**, until `reported` names them.
3. **Landings in flight**.
4. **Verdicts queued by this session** that still wait to land (`kitlib.gate.verdicts`, the entries whose `session` is it), or why the queue cannot be read. The queue keeps an entry after its branch lands, so `show` leaves out what `verdicts.expired` names against main as last fetched, with no network: a tip in main, or a branch that is gone. It only reads: every entry stays on disk. When main as fetched cannot be read, every entry is listed and a line under them says landed ones can be among them.
5. **Landed and on the hub feed**, and **Reported**: the latest 5 of each.
6. **Backlog writes**: the latest 15.

A landing is the latest `land_submit` of its branch: a new submission replaces the old one and clears its `reported` mark. It is joined with the branch's land result record at the submitted tip (`result.read`, `result.at_tip`) and, once that is final and landed, with the outcome lines of its output file (`submit.outcome_block`):

| Land record at the tip | Output's outcome lines | Section |
|---|---|---|
| none yet, of another tip, or `running` | not read | in flight |
| final, did not land | not read | to report |
| landed | the feed WARNING | to report, with the WARNING's text |
| landed | a `LANDED` line, no WARNING | landed and on the hub feed |
| landed | neither yet | in flight |
| landed | no output named, or it cannot be read | to report, saying the feed cannot be told |

The result record says nothing of the hub feed. `Lander.post_landing` says its `WARNING: the landing of <branch> was not posted to the hub feed (<why>); the landing stands` (docs/gate.md "Landing feed") in the landing's own output, the `<stem>.<run_id>.land.log` of docs/gate.md "Submitting a landing", after the last `result record:` line and before the `LANDED` line, which is what `outcome_block` returns. A successful post says nothing, so "on the hub feed" is read from a `LANDED` line with no WARNING. In the moments between the final record and the post, the output has neither.

A `reported` mark counts only on a final record at the tip. A landing in flight stays in flight, so a failed landing cannot fall into the trimmed Reported section on a mark made before it failed.

A landing whose process died leaves a `running` record, and `show` reads without finishing it, so it stays in flight until a `gate.py wait-result` finishes it `error` (docs/gate.md "Result record").

## Commands

```
python <kit>/tools/coord_state.py -C <repo> start [--slug S]
python <kit>/tools/coord_state.py -C <repo> show [--session NAME] [--no-write]
python <kit>/tools/coord_state.py -C <repo> reported BRANCH [--tip SHA]
python <kit>/tools/coord_state.py -C <repo> stop
```

| Command | What it does |
|---|---|
| `start` | Turns this session's log on: in the coordinator folder that holds its stopped log, state note or judgments, else `<agent temp>/<S>-coordinator` (S default: the main checkout's folder name). A state note not rendered by `show` is added to the judgments file first, since `show` overwrites it. Prints `STARTED <log>`, `RESUMED <log>` or `ALREADY ON <log>`. |
| `show` | Prints the note and writes it to `state-<session>.md` by a temp file and a replace. A note there that `show` did not render is added to the judgments file first, as `start` does, once however often either runs; if that fails it stays where it is (`NOT DONE`). `--no-write` prints only and touches neither file, so it does not show such a note. `--session` reads another session's: a successor's read of its predecessor. |
| `reported` | Marks BRANCH's latest submitted landing as told to the PM (with `--tip`, only when its tip starts with SHA). Prints `MARKED <branch>`. Writes nothing and exits 1 with `NOT FOUND` when the log holds no such landing, or `NOT MARKED` when it is in flight. |
| `stop` | Renames the log to `.off`. From then on the state note is written by hand again. Prints `STOPPED <off file>`. |

Exit codes: 0 done; 1 not done (`NOT ON`: no log for the session; `NOT FOUND` and `NOT MARKED`: above; `NOT DONE: <why>`: no session, a file that cannot be read or written, a repository git cannot place); 2 usage.

Known limit: an edit to a rendered note (one that still begins with its `# Coordinator state:` line, say after `stop`) is not added to the judgments file, and the next `show` overwrites it. Write such a thing in the judgments file.

## Hand-off

The old session runs `show` last, so its note is current. The successor has a session name of its own, so it runs `show --session <old session>`, which renders the old session's log against the records of now: a landing that ended after the hand-off shows as ended. The log is not carried over: a successor that starts its own begins empty, and `reported` marks only its own log's landings.
