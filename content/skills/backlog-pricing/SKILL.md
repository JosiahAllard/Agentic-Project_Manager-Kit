---
name: backlog-pricing
description: Use when you price a backlog record (PM or coordinator) - the checks that close or narrow it first, what each priority 1-5 means, tie-breaks, and who may close what.
---

# Backlog pricing

Price with `python <kit>/tools/backlog.py -C <worktree> set <ID> --priority <n> --priority-by <who>` (skill backlog-ops, step 8). A coordinator sets a first price of 2-5 on an unpriced record (`--priority-by agent`); never over a PM or owner price, never 1.

## A. Before pricing: close or narrow

Check the record against the code and the docs. Close or narrow it when it is:

- already done (`close`, naming what did it) or no longer applies (`retire`, citing the check);
- a duplicate;
- a nitpick (wording, a rename, one doc line, a test name or strength, a tidy-up);
- overengineering (a hypothetical or unobserved case, hardening against deliberate misuse or evasion, or machinery beyond the observed problem);
- dropped by the owner;
- citing no observed failure, concrete defect, cleanup (file:line) or owner request.

Narrow it rather than close it when only part of it fails.

## B. What each priority means

- p1: the owner or the PM.
- p2: blocks a p1, a red gate, or a live failure in the focus area.
- p3: a real defect or an owner feature that blocks nothing.
- p4: cleanup, docs, dead code, tooling comfort.
- p5: nice-to-have; never closed for sitting at p5.

Ties: the focus area first, then the smaller item. Never raise a record because it is easy.

## C. Who closes what

- A coordinator retires an unpriced record that fails A, citing the check (`retire <ID> --why "<check>"`; one already done is closed with `close`, not retired).
- An owner- or PM-priced record that fails A goes to the PM, who decides; a coordinator never retires it.
- A speed record closes only with before and after seconds from the next daily or full run; an agent's own local timing is not enough.
- Every lever a record names but does not take is filed as its own record before the record closes.
