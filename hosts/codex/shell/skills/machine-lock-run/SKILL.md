---
name: machine-lock-run
description: Use before any heavy work on the shared machine - builds beyond the ordinary gate, mutation or stress runs, networked end-to-end runs, renders, local-AI generation, training - to wrap it in the machine lock or its background mode, read status, and handle busy and reservation refusals.
---

# Machine lock

One machine is shared by every repo, every host's sessions, the local generators and training. Heavy work runs inside the lock; light work (ordinary gates, unit tiers) never locks. One queue, first come first served; sizes are whole cores plus gpu slots from the machine's layout; an agent run takes at most the agent cap of cores.

1. Look first: `python <kit>/tools/machine_lock.py status` (each waiting run's position and expected start).
2. Book the lock only through a kit wrapper. A kit tool that books itself (through `kitlib.lock.run.book`, or by re-running itself under `machine_lock.py run`) is a wrapper: run it directly, never under `machine_lock.py run`. The wrappers: `<kit>/tools/gate.py` (gates), `<kit>/tools/land.py` (landings), `<kit>/tools/daily_full.py` (daily full runs), `<kit>/tools/redfirst.py` (red-first runs), `<kit>/optional/comfy/comfy_gen.py` (local-AI generation), `<kit>/optional/comfy/render_glb.py` (renders). A repository's own runners and profile tools are wrappers only if they book through the kit lock themselves; the profile names them (`python <kit>/tools/doc_index.py -C <worktree> --find lock`). Every other heavy command goes through step 3.
3. Anything else heavy: `python <kit>/tools/machine_lock.py run --repo <repo> --slug <slug> --kind <kind> [--cores N] [--within THREADS] [--expect D] [--max-wait 5m | --no-wait] [--resource NAME] -- <command>`, with the kind and ports your brief assigns; `--resource` names a shared thing, such as a port block (`ports:e2e-a`), that one run at a time may hold. It waits with no limit by default. When the repo has opted in, it may route to a load node itself and prints `running on load node NAME: <why>`; `--node main` keeps it here, and a run with `--resource`, `--cores`, `--hold` or `--within`, uncommitted tracked changes or a Windows command stays here anyway. A node runs the committed HEAD from the checkout's top: untracked and ignored files are not there, and outputs are removed with the job unless `--artifact GLOB` copies them back. Training workloads (`--gpu-share training`, the pausable tier): `training.md` beside this file.
4. The kind sets the size (`<kit>/kitlib/lock/kinds.json`): engine-test, engine-run, build medium; graph-build, daily-full, cpu-eval heavy; local-ai, render medium plus the gpu; measure heavy plus the gpu. `--cores N` changes the count, up to the agent cap. Pass `--expect <duration>` when you know it: a run that ends before the runs ahead start may go first.
5. Read your threads from `MACHINE_LOCK_CPUS` (also `MACHINE_LOCK_CPUMASK`), never assume them. A container (Docker, for example): pin it to those threads (`docker run --cpuset-cpus "$MACHINE_LOCK_CPUS"`, or `docker update <container>` before `docker exec`); run its work under an in-container `timeout -k 30 <limit>` and add `--on-release "<command that kills it>"`.
6. `--hold <id>` only with a hold id the owner or PM gave you (exit 12 if it cannot take the run); `--detach` prints `<run id> pid <n>`, then follow it with `status <run id>`. `--within <threads>` (a Linux cpuset list such as 8-15, e.g. your container's `--cpuset-cpus`) keeps the run on the whole cpu slots inside it and waits for those; pair it with `--cores N` no larger than the whole slots inside.
7. Output goes to a log file; read the log when the run ends.
8. A run past its kind's `warn_min` gets a WARNING, never a kill: check it is still progressing.
9. End a run you started: `python <kit>/tools/machine_lock.py stop <run id>`.

Refusals: exit 6 busy (`--no-wait`), 8 `--max-wait` ran out: waiting is normal. Exit 11: a reservation leaves too few slots: wait or ask your coordinator, and pass `--expect` when the run is shorter than its kind's guide. Exit 2 nested: a run needing more than the outer locked run holds; run it outside. Exit 2 `--within`: the list is malformed, holds no whole slot or fewer than `--cores`, or comes with `--hold`. Exit 9 (LOST, on a node) and 14 (the job started, or its start reply never came, and gave no exit code): the job may still run; use the printed follow and stop lines, never rerun. 13: the artifacts did not come back. Exit 10 (lock not taken), 1 (`could not start the command`), 12: report. A bare `bash` is Git Bash, never WSL; none found is refused before booking: pass a full path. Build daemons a gate leaves are stopped only when that gate alone used them (machine config `machine_lock` `"daemons"`). Exclusive window declared for another session: pause every gate, build, heavy test and landing until it closes.

## Never

- Run heavy work unlocked because the queue is busy, or take an environment variable claiming a lock as proof of one.
- Truncate a locked run's live output (`head`, or any first-N filter): it kills the wrapper.
- Touch another run's lock, records or processes, or use `stop --force`, `hold`, `division` or `game`: report the run id and `status` text to your coordinator.
- Time a measurement under load without labelling it "shared machine".

Details: `<kit>/docs/lock-api.md`; read-only lock tools over MCP: `<kit>/docs/lock-mcp.md`.
