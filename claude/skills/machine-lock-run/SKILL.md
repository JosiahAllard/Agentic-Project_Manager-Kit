---
name: machine-lock-run
description: Use before any heavy work on the shared machine - builds beyond the ordinary gate, mutation or stress runs, LAN or lossy e2e, renders, local-AI generation, training - to wrap it in the machine lock or its background mode, read status, and handle busy and reservation refusals.
---

# Machine lock

One machine is shared by every repo, the local generators and training. Heavy work runs inside the lock; light work (ordinary gates, unit tiers) never locks. Sizes are whole physical cores plus gpu training slots, from the machine's layout (`status` prints it). An agent run takes at most the agent cap of cores; only a run inside an owner whole hold takes more. One queue, first come first served, no repo has priority: `status` shows each waiting run's position and expected start. A run starts ahead of its turn only when it expects to end before the runs ahead of it start, so pass `--expect <duration>` when you know it; a caller that dies leaves the queue at once. The tool is `${CLAUDE_PLUGIN_ROOT}/tools/machine_lock.py` in the kit (`${CLAUDE_PLUGIN_ROOT}/docs/lock-api.md`); the kinds table is `${CLAUDE_PLUGIN_ROOT}/kitlib/lock/kinds.json`.

## Steps

1. Look first: `python ${CLAUDE_PLUGIN_ROOT}/tools/machine_lock.py status`.
2. A tool that takes the lock itself (`tools/gate.py`, `${CLAUDE_PLUGIN_ROOT}/optional/comfy/comfy_gen.py`, the repo's own e2e or daily runners) needs no wrapper; inside a run that holds the lock it runs straight through. Check the repo's profile (`python tools/doc_index.py --find lock`) for wrappers it names.
3. Direct form: `python ${CLAUDE_PLUGIN_ROOT}/tools/machine_lock.py run --repo <repo> --slug <slug> --kind <kind> [--cores N] [--expect D] [--max-wait 5m | --no-wait] -- <command>`. It waits with no limit by default; `--max-wait` (s, m, h or d) sets one.
   - The kind sets the default size (read `${CLAUDE_PLUGIN_ROOT}/kitlib/lock/kinds.json`): engine-test, engine-run and build take the medium size; worldgraph, daily-full and cpu-eval the heavy size; local-ai and render the medium size plus the whole gpu; measure the heavy size plus the whole gpu. `--cores N` changes the count, up to the agent cap. Read the threads you got from `MACHINE_LOCK_CPUS`, never assume them.
   - `--hold <id>`: only when the owner or the PM gives you a hold's id. The run waits for it and may take every core; exit 12 if it cannot.
   - `--gpu-share training`: model training only, on local-ai or render; takes one gpu training slot instead of the card.
   - Every command gets `MACHINE_LOCK_CPUS` (a Docker cpuset list such as `0-3`) and `MACHINE_LOCK_CPUMASK`. A Docker caller passes `--cpuset-cpus "$MACHINE_LOCK_CPUS"` to `docker run`, or to `docker update <container>` before a `docker exec` (exec has no such flag).
   - Work in a container outlives a killed `docker exec` client. Run it under an in-container `timeout -k 30 <limit>` no longer than your hold, and add `--on-release "<command that kills it>"` (it runs before the slots free).
   - A Gradle or Kotlin daemon a gate run leaves behind is stopped when the gate ends, but only when every client it serves is in that gate's job; one another build is using is left running, with the reason said. MSBuild nodes and the compiler server are always left. Any other run leaves them all running for the next build. Only `gate.py`'s own booking is a gate run. The machine config's `machine_lock` `"daemons"` changes this: `"stop"` after every run, `"leave"` after none (`${CLAUDE_PLUGIN_ROOT}/kitlib/lock/site.py`).
   - A bare `bash` command runs Git Bash, never the WSL one; with no Git Bash found it is refused before the run books, so pass a full path (`${CLAUDE_PLUGIN_ROOT}/kitlib/proc/bash.py`).
   - A run past its kind's `warn_min` gets a WARNING, never a kill. Check it is still making progress.
   - Nested inside another locked run, a run needing more of a pool than the outer run holds is refused, exit 2: run it outside.
   - `--detach` runs it outside your shell and prints `<run id> pid <n>`; follow with `status <run id>`.
4. Training: `python ${CLAUDE_PLUGIN_ROOT}/tools/machine_lock.py background --repo <repo> --slug <slug> -- <launcher>`; the child waits for its slots and pauses (checkpoints and exits 75, freeing them) whenever a run due to start needs them. The trainer must use `${CLAUDE_PLUGIN_ROOT}/kitlib/lock/bg_client.py` (`should_pause()`), checking often and checkpointing promptly; `${CLAUDE_PLUGIN_ROOT}/docs/lock-api.md` has the details.
5. Send output to a log file and read the log. Never truncate a locked run's output (`head`, `Select-Object -First`): it kills the wrapper.
6. End a run you started: `stop <run id>`. Nothing else: `stop` refuses another session's run, and `--force` is not for agents.

## Refusals

- Exit 6 busy (`--no-wait`), exit 8 its `--max-wait` ran out. Waiting is normal; never start the work another way.
- Exit 11: a reservation leaves the run too few cores or gpu slots before its wait ends. Wait, or ask your coordinator; never release it. Pass `--expect <duration>` when you know the run is shorter than its kind's guide, so a scheduled reservation does not block it.
- Exit 12: the named hold cannot take the run. Exit 10: the lock could not be taken; exit 1 `could not start the command`. Report both.
- Exclusive window: when the owner or PM declares one for another session, pause every gate, build, heavy test and landing until it is closed.

## Never

Touch another run's lock, records or processes; report its run id and `status` text to your coordinator. Use `hold` or `division` or `game` unless told to. Time a measurement under load without labelling it "shared machine".
