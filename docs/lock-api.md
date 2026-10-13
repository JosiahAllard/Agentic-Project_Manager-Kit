# Machine lock API

`kitlib/lock/api.py` is the one way any UI reads or changes the machine lock. Today that means `tools/machine_lock.py` (`status --json` prints `snapshot()`) and the lock MCP server, `mcp/lock_server.py` (docs/lock-mcp.md).

- Calls are plain functions. JSON-ready dicts go in and out, and there is no transport.
- The schedule window, `reserve_ui.pyw` (kit root) (logic in `kitlib/lockui/`), reads with `snapshot()` and `history()` and makes every change with the write calls below. Owner writes go through only when the owner runs it (Who may, below).
- Every call takes an optional `store` (a `kitlib.lock.store.Store`). The default is the machine's store.
- `SCHEMA_VERSION` (now 1) changes only when a key changes meaning or goes away. Added keys leave it alone.
- Runs: a `docker exec` run (`run` or `background`) that holds no gpu slot gets `-e CUDA_VISIBLE_DEVICES= -e JAX_PLATFORMS=cpu` right after `exec`, unless the caller sets that variable with `-e`/`--env` itself (`kitlib.lock.run.docker_cpu_env`).

## Reads

| call | returns |
|---|---|
| `snapshot(store, history_days=7, since=None, until=None)` | The Snapshot below. |
| `wait_change(revision, timeout_s=30, store)` | The snapshot once the store's revision moves past `revision`, or after `timeout_s`. It polls every `settings.wait_change_poll_s`. Live use (cpu, memory) can move without a new revision. |
| `stats(store, days=7, since, until)` | The snapshot's `stats`. `since` and `until` are ISO times with their offset; a bad one raises `ApiError` (`invalid`). |
| `history(store, days=7, since=None, until=None)` | The runs that ended in `[since, until)` (default: the last `days`), oldest first, one store history line each (`held_s`, `wait_s`, `partial`, `background`). A bad time or a non-numeric `days` raises `ApiError` (`invalid`). |
| `run_record(id)` / `hold_record(id)` | One run or hold, live (`live: true` / `live_record: true`) or ended, or `None`. When the holds cannot be read, `hold_record` returns the write error result (`store`, below), never `None`. An id must be one the lock makes (`kitlib.lock.store.ID_RE`, whole string, `r-` for a run, `h-` for a hold); any other is the `invalid` error result, since the id names a store file. |

### Snapshot

`schema_version`, `revision` (the store's write counter), `at`, `store`, plus:

- **`hardware`**: `threads`, `thread_ids`, and `groups` (processor groups; more than 64 logical processors).
  - `cores` [{threads, smt, efficiency}].
  - `ccds` [{index, threads, cpu_slots, preferred, game_pick}], one per L3 domain. `preferred` marks the domain holding the best efficiency or scheduling class. `game_pick` marks the one game mode takes.
  - `preferred_threads`.
  - `memory_mb` {total, free}.
  - `gpus` [{index, name, vendor, vendor_id, vram_mb, shared_mb, source, default_share}], any vendor, from DXGI or CIM.
  - `source` and `problems`.
- **`layout`**: the layout in force.
  - Its shape: `key`, `threads`, `cpu_slots`, `cpu_slot_threads`, `slots_cpu`, `gpu_slots`, `gpu_cards`, `gpus`, `half`, `agent_cap`, `medium_cores`, `l3_slots`.
  - `division` (in force), `default_division`, and `wanted_division` / `wanted_key` (the owner's machine.json division on this hardware), and `owner_division` (only the keys machine.json sets, `{}` when none; a dialog prefills from it so saving pins no derived key).
  - `source` (`config` or `default`) and `warning`.
  - `hardware_change` {changed, pending}. When `changed` is true, the wanted layout differs from the one in force, and it is adopted once nothing is live or queued. `pending` is true while it waits for that.
  - A run record's `layout` is `layout.slot_key`, not `key`: a label of the slot numbering only, `t<threads> g<cards> cpu <cpu slots> x <threads per slot> gpu <gpu slots> m<hash>`.
  - What the label is: `g` is the layout's `gpus`, the cards of `hardware.gpus` that `layout.derive` gives a share (the division's `gpu_share`, else each card's `default_share`). `gpu` is `gpu_slots`, which is 1 with no shared card (the placeholder slot). The hash covers the cpu slots only. `tests/lock/test_layout.py` pins the text for one card, two cards and none.
  - What it is not: this kit never reads or compares it. `run.book` counts slots from layout.json (`layout.in_force`), and a live record with another label never refuses a booking (`tests/lock/test_layout_mismatch.py`). It does not say which cards the gpu slots are; `gpu_cards` does.
  - The format is that of the older kit's `machine_lock.py`, kept for the records and for any such tool still installed and sharing the store: that tool refuses a run while a live record holds a string other than its own. Nothing in this kit checks that another tool counts cards or gpu slots the same way.
- **`slots`**: {cpu: [...], gpu: [...]}, one per slot.
  - {slot, holder (run id), background, hold (the hold id covering it now), hold_owner}.
  - cpu slots add `threads` and `pinned`; gpu slots add `card`.
- **`runs`**: every live run (waiting, held or background).
  - The record's id, state, repo, slug, kind, note, pid, ctime, since, held_since, expect_s, expect_from, hold, parent, pinned, pin and pin_note.
  - `pin` is "pinned", null (no pin) or "overridden": an outer job's affinity limit won, so `pinned` is false and `threads` is what the command really runs on (`kitlib.proc.job.Job.check_pin`).
  - `cores`, `gpus` and `tree` (the pid tree).
  - `usage` {cpu_s, peak_cpu_cores, peak_mem_mb, peak_commit_mb, cpu_cores_now, mem_mb_now}.
  - `gpu_mem_mb`, from NVIDIA only; null elsewhere.
  - `expected_end` and `overrun` {s, planned_end}.
  - Waiting runs add `position`, `est_start`, `backfill` and `moved`.
- **`queue`**: `entries` in plan order [{id, position, ticket, repo, slug, kind, needs, since, expect_s, est_start, backfill, parked, blocked_by, hold, moved, why}], and `error`.
  - `moved` is {by, name, at, seq} when the owner moved the entry to the top (`move_to_top`), else null.
  - `why` is the reason the entry waits.
  - `entries` is null while the queue is unreadable. Then no run takes slots and a new run exits 10 (fail closed).
- **`holds`**: [`kitlib.lock.holds.entry`], and `holds_error`.
  - The record (without ancestors, with its `job`), `start_local`, `end_local`, `covers`, `slots_fixed` and `live`.
  - A hold file that cannot be read, or is not a hold, is never read as absent (`kitlib.lock.holds.HoldsUnreadable`): `holds` is empty and `holds_error` names the file. Until it reads, no run takes slots, a waiting run keeps its place and a new run exits 10 (fail closed).
  - Agent holds add `state` (upcoming, open or claiming) and `lapses_at`.
- **`game`**: `kitlib.lock.game.state`, with fields {enabled, games [{exe, name, pin, anti_cheat}], running, hold, slots, would_take, pinned, anti_cheat_services}.
- **`kinds`**: the kinds table, with `kinds_error`. A kind's `cpu` is a whole number of cpu slots or the alias `medium` or `heavy` (kitlib/lock/kinds.py). A whole number past the layout's `agent_cap` empties `kinds` and sets `kinds_error`.
- **`stats`**: {range {since, until}, rows [{kind, repo, runs, done, failed, interrupted, held_s {median, p90, max}, wait_s {median, max}, cpu_s, peak_cpu_cores, peak_mem_mb}]}.
  - The rows come from the history lines (`Store.history_records`) in the range.
  - `failed` means the command exited non-zero. `interrupted` means it was stopped, cancelled or killed.

## Writes

Every write takes `actor`. On the machine's store only its `name` is used: the write reads who is asking from the calling process's tree (`api.who`, below). On a scratch store (tests) `actor` is taken as given. A write never raises for a refusal, and returns one of:

```
{"ok": true,  "result": {...}, "snapshot": {...}}
{"ok": false, "error": {"code": "...", "message": "...", "field": "..." | null}, "snapshot": {...} | null}
```

Error codes:

| code | meaning | CLI exit |
|---|---|---|
| `invalid` | Bad input; `field` names the input key at fault | 2 |
| `forbidden` | `authorize` refused it | 2 |
| `not_found` | No live run or hold of that id | 1 |
| `conflict` | A hold is in the way, or (`move_to_top`) the run has started | 11 |
| `busy` | Agent holds leave an agent hold too few slots | 6 |
| `store` | The store could not be read or written | 10 |

| call | who | result |
|---|---|---|
| `set_division(actor, division)` | owner | {applied, pending}. `division` is a dict of `layout.DIVISION_KEYS`; null means every default. It is checked against this machine (`layout.derive`) before it is written. Keys starting with `_` are notes, not division keys: notes already in machine.json are kept (one the division also names takes its value), and machine.json is removed only when it would hold neither keys nor notes. |
| `hold_create(actor, spec)` | owner (owner holds); anyone (agent holds) | {hold, notes}. spec: `owner` (owner or agent), `scope` (owner: whole, half or slots), `start` (ISO with its offset, `Z` or `+hh:mm`; default now), `end` or `for_s`, `note`, `job` (optional: `OWNER` for an owner hold, repo/slug for an agent hold, and any other is invalid). whole names nothing more: it is the owner's, for the owner's own runs; slots adds `slots` {cpu, gpu}; agent adds `repo`, `slug`, `kind`, `gpu_share`. Game holds are game mode's. |
| `hold_edit(actor, id, changes)` | owner; an agent its own agent holds | {hold}. Owner holds take start (before the hold starts), end and note. Agent and game holds take end and note. |
| `hold_end(actor, id)` | owner; an agent its own agent holds | {hold}. A game hold comes back while the game runs, so switch game mode off instead. |
| `set_games(actor, games)` | owner | {games}. [{exe, pin?, name?}]. A missing `pin` defaults off for a title known to use Vanguard, EasyAntiCheat or BattlEye, and on otherwise. |
| `set_game_mode(actor, enabled)` | owner | {enabled}. Off ends a live game hold at once. |
| `end_run(actor, id)` | owner; an agent its own runs | {id, requested}. `id` is a run id (`ID_RE`, `r-`), else `invalid`. It writes the run's stop request and does not wait (the CLI's `stop` waits up to `settings.stop_s`). |
| `move_to_top(actor, id)` | owner | {id, moved {by, name, at, seq}, position}. Moves a waiting run to the front of the queue (`queue.plan`): only a hold's claimer or a nested run plans ahead of it, a later move goes ahead of an earlier one, and no run planned ahead of its turn (backfill) may start where it would delay it. Runs holding slots keep them: nothing is stopped or preempted. The run's record keeps `moved` once it starts. `invalid` for a bad id, `not_found` for a run that is not live (the message says how an ended one ended), `conflict` for one that holds slots. The CLI is `machine_lock.py top ID`; the lock window has a Move to top button on each waiting run. |

## Who may (the one hook)

- `authorize(actor, action, target)` decides every write. It returns None, or the reason for refusing.
- Who is asking is `api.who(actor, store)`. On the machine's store it is `store.caller()`, read from the calling process's tree: the role is agent when any ancestor is an agent host (`store.agent_hosts()`: every hosts.json profile's `process_names`, today claude.exe, which is both Claude Code and the Claude desktop app, and node.exe, plus codex.exe until Codex is registered), or when the tree or hosts.json cannot be read. The session id shown is the first set one of the profiles' `session_env` variables. Environment variables, a role, `force` or a session id in `actor` change nothing.
- So the owner's commands run from a terminal outside the Claude desktop app. A command run from the app's terminal panel has the app above it and is an agent's.
- The owner may do everything. "Owner" means `is_owner(actor)`, which calls the owner check.
- The default check is `default_owner_check`: `role == "owner"` with no agent host (`host`; an actor made before that key was renamed carries it as `claude`, which is still read, `store.actor_host`).
- `set_owner_check(fn)` replaces it, and `set_owner_check(None)` restores the default. This is the only place ownership is decided.
- An agent owns a record when its nearest agent host (its Claude Code process, from `store.caller`) is among the record's ancestors (`store.owns`). A session id, which `snapshot()` shows, grants nothing.
- Ending another session's run is the owner's; the CLI's `stop --force` gives an agent nothing. A run whose wrapper has died is reaped by the lock itself.

## Defaults the owner may swap

- **Overrun (owner decision):** a holder past its expected end is planned to end at now plus half its expected time, re-checked on every pass. The decision lives in `kitlib.lock.queue.holder_end`.
- **Whole holds (owner decision):** every hold names its job, and a whole hold is the owner's (job `OWNER`). Only a run the owner started may run inside it: the run record's `role`, read from its process tree (`kitlib.lock.run.run_role`). The earlier open whole hold for agents' runs is gone. The one place this is decided is `kitlib.lock.holds.admits`.

## Giving back slots

A held run can hand back cpu slots its command no longer needs, while the command runs on, so a queued run starts on them at once (`kitlib/lock/giveback.py`; its docstring has the whole protocol). Booking only shrinks: nothing given back is taken again, and the queue order and pre-emption are unchanged.

- Offered to one command only: `machine_lock.py run`'s command (`kitlib.lock.run.run_command`) and a gate's last step (`kitlib.gate.runner.run_steps`), through `MACHINE_LOCK_GIVE_BACK`, the path of the request file `active/<run id>.giveback`. A nested run, a background run and every other command get no offer.
- A run under the installed monolith wrapper (the installed `machine_lock.py`) gives back nothing, because only kit-wrapped runs offer slots back; `kit_tests` then logs that it had no offer.
- The command asks with `giveback.Offer.give(cpu)`, or `giveback.drainer(say)` for a pool that drains; the wrapper serves it while it waits on the command (`Slot.serve_give_back`).
- Refused, changing nothing: a malformed request (a `gpu` that is not a list counts), a gpu slot, a slot the run does not hold, every slot it holds (one stays), a run that is nested or not pinned, and an asker that is not a live process in the run's job. The refusal is the record's `give_back_refused` {id, why, at}.
- Order: the job's affinity narrows to the slots kept first (`kitlib.proc.job.Job.narrow`; every process in the run's job moves, nested jobs too, but not a container that `docker exec` or `docker run` started: it keeps the cpuset it started with), then under the `queue` mutex the run record (`cores`, `slots`, `has`, pin fields, `given_back` {id, cpu, cores, cpumask, cpuset, at}), then the queue entry's `cores`. Later commands get the narrower `MACHINE_LOCK_CPUSET`, `MACHINE_LOCK_CPUS` and `MACHINE_LOCK_CPUMASK`.
- Fail safe: a failed write puts the record (in memory and on disk) back as it was, and the record and queue entry keep every slot booked. The job's pin is widened back (`Job.repin`), which moves its processes back onto every slot too; if Windows refuses that, the refusal says the job stays narrowed. The queue entry, which every planner reads, is written last, so a wrapper that dies between the two leaves the slots counted held, never handed out twice.
- The installed `machine_lock.py` reads a shrunk record and entry as any other. The request file is not a `.json`, so neither tool reads it as a run record; `Store.live_runs(prune=True)` removes an orphaned one.
- The history line keeps `given_back`.

## Exclusive resources

`kitlib/lock/resource.py` keeps a second queue in the same store, `resources.json`, for things one caller holds at a time and that take no slot: a repository's landing (docs/gate.md "The landing queue"). It is not in the snapshot and has no write call here; its callers use the module directly.

- `join(store, resource, label)` puts a `Turn` at the back; `try_take()` returns `[]` when the caller holds the resource, else the live entries ahead of it, holder first; `wait(timeout_s)` repeats it for `timeout_s` from the first look that finds entries ahead; `leave()` gives the turn up and never raises. `entries(store, resource)` reads the queue without writing.
- First come, first served: the lowest ticket for a resource holds it, nothing goes ahead of an earlier entry, and nothing takes a resource from its holder.
- An entry stands while its process holds the entry's run mutex (`store.run_mutex`), as a run's queue entry does; a caller that dies leaves an entry the next look drops (`store.run_dead`).
- It is read and written only under the store mutex `resources`. A file that exists and cannot be read raises `ResourceUnreadable` after `settings.queue_read_s`; it is never read as an empty queue.
