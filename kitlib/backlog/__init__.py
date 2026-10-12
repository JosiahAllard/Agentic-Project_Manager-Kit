"""kitlib.backlog: the backlog library behind tools/backlog.py (docs/backlog.md,
docs/backlog-store.md). Logic only: no argparse, no printing, no exit.

  errors    Refusal, Usage, NotFound, Failure (each with its exit code)
  scope     scope members, collision (undecidable counts as a collision), area
  record    parse, the schema, record-local validation, render, apply_changes
  store     Snapshot, the ref/branch/main backends, the compare-and-swap write loop
  ledger    what a record's own ruling fields allow (a live hold, a spent ruling), and
            `orphan_report`, which reads the ledger file `ledger.py check` hands it; a backlog
            write (plan.py `_check_ruling`) and `check` read no ledger file
  offer     partition, rank, in flight, requires, the `next` offer
  check     every `check` line with its own denominator
  plan      the write verbs as plans: add, set, block, unblock, pull, release, close, retire
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
