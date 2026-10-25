# Machine lock: training workloads

- Read your threads from `MACHINE_LOCK_CPUS` (also `MACHINE_LOCK_CPUMASK`), never assume them. Docker: pass `--cpuset-cpus "$MACHINE_LOCK_CPUS"` to `docker run`, or `docker update <container>` before `docker exec`; run container work under an in-container `timeout -k 30 <limit>` and add `--on-release "<command that kills it>"`.
- `--gpu-share training` only for model training on local-ai or render.
- Training, the pausable tier (not `run`'s detached form; that is `--detach`): `python <kit>/tools/machine_lock.py background --repo <repo> --slug <slug> -- <launcher>`; the trainer polls `should_pause()` in `<kit>/kitlib/lock/bg_client.py` and checkpoints and exits 75 when a due run needs its slots: it yields to due runs instead of holding its slots.
