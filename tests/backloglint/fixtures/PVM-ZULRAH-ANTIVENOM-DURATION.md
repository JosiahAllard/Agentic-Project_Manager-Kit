id: PVM-ZULRAH-ANTIVENOM-DURATION
area: artifacts
priority: 3
priority_by: owner
priority_ruling: R-9001
effort: s
state: open
blocked_on: -
scope: artifacts/models/env/zul_shim.c
scope: artifacts/models/env/test_zul_rules.c
summary: Zulrah sim anti-venom lasts 300 ticks (anti-venom+ prose value; zul_shim.c ZUL_ANTIVENOM_DURATION, obs scale at :35), but the kit's extended anti-venom+ is 630 ticks a dose (gamma research vorkath-logs/FACTS.md Z2). Fights run ~193 ticks, so it rarely binds: set 630 (or model the kit's serpentine helm instead), rescale the obs, rule test. Changes the zulrah sim id

Header copied from a repository's docs/backlog (the incident behind backlog_lint).
