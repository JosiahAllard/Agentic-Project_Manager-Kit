id: EX-SIM-BATCH-STATE
area: artifacts
priority: 3
priority_by: agent
effort: s
state: open
blocked_on: -
scope: artifacts/models/env/batch.py
scope: artifacts/models/env/batch_shim.c
summary: SimBatch keeps process-wide state: a batch's starting obs depend on batches run earlier in the same process (the recorder's config A, n=16 seed 77, has the same step-0 hashes on both builds when run alone and different ones after config B's 4000 steps in the same process; seen on both the base and the newer build; cause not found; evidence in the recorder log addendum, gamma research folder). Possibly related: replay mismatches (9 of 458 and 68 of 6881 ticks), suspected stale shared route-cache memory in copied contexts

Header copied from a repository's docs/backlog (the incident behind backlog_lint).
