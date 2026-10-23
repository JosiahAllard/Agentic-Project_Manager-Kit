id: EX-SIM-SHARED-STATE
area: artifacts
priority: 3
priority_by: agent
effort: s
state: open
blocked_on: -
scope: artifacts/models/env/batch.py
scope: artifacts/models/env/batch_shim.c
summary: SimBatch keeps process-wide state: a batch's starting obs depend on batches run earlier in the same process (config P, n=8 seed 5, gives the same step-0 hashes on two builds when run alone and different ones after config Q's 900 steps in the same process; cause not found; evidence in the run log addendum, gamma research folder). Possibly related: replay mismatches (3 of 120 and 21 of 2400 ticks), suspected stale shared cache memory in copied contexts

Invented fixture shaped like a backlog record header.
