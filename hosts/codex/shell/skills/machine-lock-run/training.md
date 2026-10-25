# Machine lock: training workloads

- `--gpu-share training` only for model training on local-ai or render.
- Training, the pausable tier (not `run`'s detached form; that is `--detach`): `python <kit>/tools/machine_lock.py background --repo <repo> --slug <slug> -- <launcher>`; the trainer polls `should_pause()` in `<kit>/kitlib/lock/bg_client.py` and checkpoints and exits 75 when a due run needs its slots: it yields to due runs instead of holding its slots.
