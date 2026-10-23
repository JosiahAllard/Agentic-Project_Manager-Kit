"""kitlib.backlog: the backlog library behind tools/backlog.py (docs/backlog.md,
docs/backlog-store.md). Logic only: no argparse, no printing, no exit.

  errors    Refusal, Usage, NotFound, Failure (each with its exit code)
  scope     scope members, collision (undecidable counts as a collision), area
  record    parse, the schema, record-local validation, render, apply_changes
  store     Snapshot, the ref/branch/main backends, the compare-and-swap write loop
  ledger    the rulings ledger's entries, re-exported for their readers; no backlog write and
            no `check` reads a ledger file
  policy    tools/backlog.py's role check: a write's rows (roles.py ACTIONS) and who may take them
  offer     partition, rank, in flight, requires, the `next` offer
  check     every `check` line with its own denominator
  plan      the write verbs as plans: add, set, block, unblock, pull, release, close, retire,
            delete, set-status, unpark, clear-review
  retarget  `backlog.py retarget`: a moved path rewritten across every record in one commit
  landable  may a branch land without leaving a dead pull
  at_landing  the store at `gate.py land`: the ancestry refusal and closing the landed pulls
  migrate   `backlog.py migrate`: main's records become a new store's root commit
  opener    open_repo, open_store (the configured store from a worktree), planner (verb -> plan),
            retarget_dry (the Change a `retarget` without `--apply` would write)

`open_store` is also `kitlib.backlog.open_store`: opener's own function, imported on first use
(`__getattr__` then keeps it in this module's globals) so that importing another module of this
package does not load the config and plan code.
"""


def __getattr__(name):
    if name == 'open_store':
        from .opener import open_store
        globals()[name] = open_store
        return open_store
    raise AttributeError('module %r has no attribute %r' % (__name__, name))
