id: EX-SIM-BUFF-DURATION
area: artifacts
priority: 3
priority_by: owner
priority_ruling: R-9001
effort: s
state: open
blocked_on: -
scope: artifacts/models/env/sim_shim.c
scope: artifacts/models/env/test_sim_rules.c
summary: Sim buff lasts 300 ticks (the base potion's prose value; sim_shim.c BUFF_DURATION, obs scale at :35), but the extended potion is 630 ticks a dose (gamma research notes FACTS.md Z2). Fights run ~193 ticks, so it rarely binds: set 630 (or model the helm instead), rescale the obs, rule test. Changes the sim id

Header copied from a repository's docs/backlog (the incident behind backlog_lint).
