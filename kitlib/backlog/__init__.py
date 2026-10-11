"""kitlib.backlog: the backlog library behind tools/backlog.py (docs/backlog.md,
docs/backlog-store.md). Logic only: no argparse, no printing, no exit.

  errors    Refusal, Usage, NotFound, Failure (each with its exit code)
  scope     scope members, collision (undecidable counts as a collision), area
  record    parse, the schema, record-local validation, render, apply_changes
  store     Snapshot, the ref/branch/main backends, the compare-and-swap write loop
  ledger    the PM rulings ledger, read only when the machine config names one
  offer     partition, rank, in flight, requires, the `next` offer
  check     every `check` line with its own denominator
  plan      the write verbs as plans: add, set, block, unblock, pull, release, close, retire
  retarget  `backlog.py retarget`: a moved path rewritten across every record in one commit
  landable  may a branch land without leaving a dead pull
  at_landing  the store at `gate.py land`: the ancestry refusal and closing the landed pulls
  migrate   `backlog.py migrate`: main's records become a new store's root commit
"""
