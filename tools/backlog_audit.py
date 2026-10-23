"""backlog_audit.py: the backlog store's history read against the audit log, for the PM's digest.

  backlog_audit.py [-C DIR] [--days N] [--no-fetch] [--all]

Reads the store `.kit.json` "backlog".store names in DIR (a ref or branch store, or a folder store
in the PM data worktree) for the last N days (default 1) and the machine's audit log,
<pm_data>/audit/backlog.jsonl. It prints one line per finding:
  UNAUDITED  a store change whose rows carry an AUDITED risk (kitlib/backlog/roles.py) and that no
             log line names by its store commit
  UNKNOWN    a store commit whose subject no backlog.py verb wrote
  UNMAPPED   a log line in the window whose role is `-` or not in the role map
and counts the writes that add no line by design (the landing's close, the Action Hub's, the daily
full run's pricing). `--all` also lists the changes a line covers and those by-design writes. It
writes no record, no log line and no checkout file. By default a ref or branch store is fetched
first: that rewrites the local store ref, and the first time it may add the store's fetch refspec
to the remote in .git/config. --no-fetch skips both. The logic is kitlib.backlog.audit_check;
docs/backlog.md "Audit log".
Exit codes: 0 OK, 1 findings, 2 USAGE or NOT CHECKED (no pm_data, the store or the log cannot be
read, or a folder store outside the PM data worktree).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.backlog import audit_check                             # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description='Cross-check the backlog store against the audit log.')
    ap.add_argument('-C', dest='root', default='.', help='the checkout whose store to read')
    ap.add_argument('--days', type=int, default=1, help='the window in days (default 1)')
    ap.add_argument('--no-fetch', action='store_true', help='read the local store ref, no fetch')
    ap.add_argument('--all', action='store_true', help='also list covered and by-design changes')
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if a.days < 1:
        print('USAGE: --days must be at least 1', file=sys.stderr)
        return 2
    code, lines = audit_check.run(os.path.abspath(a.root), days=a.days, fetch=not a.no_fetch,
                                  show_all=a.all)
    for line in lines:
        print(line, flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
