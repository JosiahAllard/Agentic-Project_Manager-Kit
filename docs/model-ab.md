# Model A/B results

Which model runs which agent is set in docs/ROSTER.md; this page records the measurements behind the choices. Cost is tokens times each model's per-token price over every API call of a run; raw tokens (cache reads are over 90% of them) are secondary. One sample per case, so variance is unmeasured.

| Model | Input | Output | Cache read | Cache write |
|---|---|---|---|---|
| Sonnet 5.5 | $2.00/M | $10/M | $0.20/M | $2.50/M |
| Haiku 5.5 | $0.10/M | $0.50/M | $0.01/M | $0.125/M |

Input and output are list prices; the cache rates are assumed at 0.1x (read) and 1.25x (write) of input.

## 2026-10-10: Haiku against Sonnet on lander, records and sync

48 runs: 8 past briefs for each of three agent types, replayed on each model in throwaway clones (no real repo touched, no push, no build or test, landers stop at the verdict). Correct = matches what actually happened.

| Type | Correct (Sonnet / Haiku) | Rule-break runs (S / H) | Cost, 8 cases (S / H) | Haiku share of cost | Raw tokens (S / H) |
|---|---|---|---|---|---|
| Lander | 6/7 / 6/7 (L1 scored for neither) | 0 / 0 | $0.513 / $0.034 | 6.6% | 0.70M / 1.18M |
| Records | 8/8 / 4/8 complete | 8 / 0 | $1.021 / $0.079 | 7.7% | 2.56M / 4.21M |
| Sync | 8/8 / 7/8 | 2 / 0 | $1.202 / $0.095 | 7.9% | 3.25M / 5.72M |
| All | | | $2.736 / $0.208 | 7.6% | 6.52M / 11.11M |

Haiku used about 1.7x the raw tokens and cost about 13x less. Misses: Haiku lander L6 refused a clean merge (read lander.md literally); Haiku records R4-R7 would not hand-edit a backlog record (no `set` verb); Haiku sync S8 stopped on the cross-checkout guard (partly a replay cwd artefact); Sonnet lander L8 kept both sides of a DECISIONS.md conflict.

Decisions: records and sync viable on Haiku under conditions (records once the backlog tool has `set`; sync for clean or doc-level merges, high-risk-path conflicts stay on Sonnet or above). The lander decision follows from the re-run below.

## 2026-10-10: lander re-run with the clarified lander.md

L6 (clean merge, right answer GO) and L8 (DECISIONS.md conflict, right answer STOP) replayed with the kit's lander.md and the land-branch skill; round 1 on the prompt before the precedence line, round 2 after it. Cost per run, $.

| Run | Round 1 | Round 2 |
|---|---|---|
| L6 Haiku medium | GO ok, .0065 | GO ok, .0119 |
| L6 Haiku high | STOP miss, .0092 | GO ok, .0148 |
| L6 Sonnet medium | GO ok, .190 | GO ok, .203 |
| L8 Haiku medium | GO miss, .0093 | STOP ok, .0123 |
| L8 Haiku high | GO miss, .0128 | STOP ok, .0146 |
| L8 Sonnet medium | GO miss, .129 | GO miss, .186 |

Round 2: Haiku followed the definition's "any conflict means STOP, even if the brief gives conflict rules"; Sonnet followed the brief's keep-both rule. Method: general-purpose agent with the definition and skill pasted in, effort set explicitly; the 48-run lander runs used the lander agent at effort low.

**Decision: lander -> Haiku, effort medium** (about 6.6% of Sonnet's cost; round 2 correct 4/4 on Haiku against 1/2 on Sonnet, medium matching high at lower cost).

## Reviewer A/B on planted bugs (final, 2026-10-10)

8 diffs (6 with 10 planted bugs, 2 clean), one review per model per diff, 24 reviews.

| | Sonnet high | Opus default | Haiku high |
|---|---|---|---|
| Plants caught | 7/10 | 9/10 | 8/10 |
| False alarms | 1 | 0 | 0 (2 overstated severity) |
| Extra real defects found | 6 | 7 | 9 |
| Clean diffs correct | 2/2 | 2/2 | 2/2 |
| Cost per review (price-weighted, understated: thinking hidden) | $0.34 | $1.11 | $0.23 |
| Raw tokens per review | 394k | 978k | 974k |
| Wall time per review | 88s | 180s | 222s |

Reviewer costs use other assumed rates than the price table above: Opus $5/$25, Sonnet $3/$15, Haiku $1/$5 per M (input/output), cache read 0.1x and write 1.25x of input. They are not comparable with the costs in the tables above.

Missed: Sonnet B2 doc contradiction, B3 silent data loss, B10 removed refusal; Opus B8 silent data loss; Haiku B8, B10. Every arm missed at least one silent data loss; a second reviewer on a different model covers more. Caveats: 10 bugs, one run per cell (1-2 catch gaps are noise); plants small and local; no tests run; wall time confounded by concurrency.

Decision: pending (owner).

## Haiku coding A/B (2026-10-10)

16 past kit briefs re-run on Haiku 5.5 in throwaway worktrees at each brief's original base, against the historical run on its original tier (Sonnet or Opus). Each Haiku result got one read-only Sonnet review. Cost is price-weighted, authors only (the historical review is excluded).

| | Cost | Raw tokens |
|---|---|---|
| Haiku authors | $7.30 | 117.6M |
| Sonnet reviews of Haiku work | $3.30 | |
| Haiku plus review | $10.60 | |
| Historical authors | $56.59 | 121.0M |

Haiku plus review is about 19% of the historical cost; raw tokens look equal because Haiku reads far more through cheap cache.

| Work type | Verdict |
|---|---|
| Docs plus code | Haiku can take |
| Small fixes, logic, features, refactors | only with review |
| Guard and parser rules, lock and concurrency, test writing from a premise | no |

Failure patterns: ran tests outside the machine lock in 8 of 16 cases (in-place mutants or pre-change runs in 2); left work uncommitted in 4; did not question stale premises; missed invariants that cut across the change. Preconditions for any wider share: mandatory review and a hard lock-tool check.

Caveats: one run per case; scored against historical tests, which mostly measure interface match rather than correctness.

Tier change: not decided (owner).
