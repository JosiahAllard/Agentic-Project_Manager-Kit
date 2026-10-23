id: EX-SIM-TIMEOUT-DEFAULT
area: artifacts
priority: 3
priority_by: owner
priority_ruling: R-9001
effort: s
state: open
blocked_on: -
scope: artifacts/models/env/sim_shim.c
scope: artifacts/models/env/test_sim_rules.c
summary: Sim timeout defaults to 300 ticks (the figure in the older spec; sim_shim.c TIMEOUT_TICKS, obs scale at :35), but the current spec says 630 ticks. Runs last about 190 ticks, so it rarely binds: set 630, rescale the obs, add a rule test. Changes the sim id

Invented fixture shaped like a backlog record header.
