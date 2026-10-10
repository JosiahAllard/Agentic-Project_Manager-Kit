"""backlog_lint.py: backlog records `next` offers to a session that cannot take them.

  backlog_lint.py [--repo NAME] [--config PATH] [--offer SESSION [--cap N | --next-output FILE]]

For each repository in the "backlog_lint" key of ~/.claude-pm-kit/config.json (or --config),
reads its published records (the store its .kit.json names, or the configured folder on main)
and prints one line per finding with the fix:
  session  a mapped area's record with no or another `session:`
  hold     a record that needs a live client or the owner's go and has no hold
  offer    (--offer S) an item the capped `next --session S` would offer that S cannot take;
           S "none" is the untagged session. Without --next-output the offer is approximate
           (rank order, no scope collisions or branch claims); with it, the ids are read from a
           saved `next` printout. A record on an offer line is not repeated as a session or hold line.
Each fix is a command to run as printed once <worktree> is filled in (a worktree of the repo; a
main checkout refuses write verbs). Read-only: it never edits a record. Exit 0 clean, 1 findings, 2 config or read error.
See docs/backlog-lint.md.
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import backloglint                                     # noqa: E402


def run(cfgs, names, offer=None, cap=5, reader=None, out=print, offered_ids=None):
    """Lint each named repo; returns the exit code. reader(cfg) -> a reader (GitReader by default).
    offered_ids: the real offer (from --next-output) instead of the approximation."""
    reader = reader or (lambda c: backloglint.GitReader(c.root, c.store, c.ref))
    total = 0
    for n in names:
        c = cfgs[n]
        if not c.root:
            out("%s: no root configured" % n)
            return 2
        try:
            records = reader(c).records()
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
            out("%s: could not read records: %s" % (n, e))
            return 2
        found = backloglint.lint(records, c)
        if offer is not None:
            offered, extra = backloglint.offer_findings(records, c, offer, cap, offered_ids)
            found = extra + backloglint.without(found, [f.id for f in extra])
            how = "offers (from next output)" if offered_ids is not None else "cap %d, would offer (approx.)" % cap
            out("%s: next --session %s, %s: %s" % (n, offer, how, ", ".join(r.id for r in offered) or "nothing"))
        cs = backloglint.counts(found)
        out("%s: %d records, %d findings%s" % (n, len(records), len(found),
                                                ("".join("; %d %s" % (v, k) for k, v in sorted(cs.items())))))
        for f in found:
            out("  " + f.line())
        total += len(found)
    return 1 if total else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Flag backlog records next offers to a session that cannot take them.")
    ap.add_argument("--repo", action="append", help="lint only this repository (repeatable)")
    ap.add_argument("--config", help="config file (default ~/.claude-pm-kit/config.json)")
    ap.add_argument("--offer", metavar="SESSION", help='also check what next would offer SESSION ("none" = untagged)')
    ap.add_argument("--cap", type=int, default=5, help="the offer's cap (default %(default)s)")
    ap.add_argument("--next-output", metavar="FILE",
                    help="with --offer: a saved `backlog.py next` printout to take the real offer from")
    a = ap.parse_args(argv)
    if a.cap < 1:
        print("--cap must be at least 1", file=sys.stderr)
        return 2
    offered_ids = None
    if a.next_output:
        if a.offer is None:
            print("--next-output needs --offer", file=sys.stderr)
            return 2
        try:
            with open(a.next_output, "rb") as f:
                offered_ids = backloglint.parse_next_output(backloglint.decode_next_output(f.read()))
        except (OSError, ValueError) as e:
            print("cannot read --next-output %s: %s" % (a.next_output, e), file=sys.stderr)
            return 2
    try:
        cfgs = backloglint.load_config(a.config)
    except backloglint.LintConfigError as e:
        print("config: %s" % e, file=sys.stderr)
        return 2
    if not cfgs:
        print("no backlog_lint config (see docs/backlog-lint.md)", file=sys.stderr)
        return 2
    names = a.repo or sorted(cfgs)
    missing = [n for n in names if n not in cfgs]
    if missing:
        print("not configured: %s" % ", ".join(missing), file=sys.stderr)
        return 2
    if offered_ids is not None and len(names) != 1:
        print("--next-output is one repository's offer: give --repo", file=sys.stderr)
        return 2
    return run(cfgs, names, a.offer, a.cap, offered_ids=offered_ids)


if __name__ == "__main__":
    sys.exit(main())
