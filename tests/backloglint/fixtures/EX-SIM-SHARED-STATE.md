id: EX-SIM-SHARED-STATE
area: artifacts
priority: 3
priority_by: agent
effort: s
state: open
blocked_on: -
scope: artifacts/models/env/batch.py
scope: artifacts/models/env/batch_shim.c
summary: SimBatch keeps process-wide state: a batch's starting obs depend on batches run earlier in the same process (config A, n=16 seed 77, gives the same step-0 hashes on both builds when run alone and different ones after config B's 4000 steps in the same process; cause not found; evidence in the run log addendum, gamma research folder). Possibly related: replay mismatches (9 of 458 and 68 of 6881 ticks), suspected stale shared cache memory in copied contexts

Invented fixture shaped like a backlog record header.
