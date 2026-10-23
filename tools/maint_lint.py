"""maint_lint.py: the maintenance lint of one repository, read-only (docs/maint-lint.md).

  maint_lint.py [-C DIR] [--dry-run] [--result FILE] [--out FILE]

Runs every check of kitlib.maintlint on the checkout DIR: keyword holds, unpriced records, dead
pulls, dead blockers, duplicates, parked records whose hub question is answered, the backlog
audit (7 days, no fetch) as counts, body, files and scope paths, the worktrees the nightly sweep
leaves, and the decision ledger as counts. No check flags anything for its age
alone. Every finding prints the command that fixes it; nothing is run, and no record, ledger,
worktree or git ref is written.

Without --dry-run it writes the result (default `<pm_data>/maint-lint/<repo>/result.json`, beside
the backlog's audit/ folder) and the page (--out, default page.html beside the result), and prints
the two paths and the counts. With --dry-run (--out is then required) it writes only the page to
--out, never result.json, and prints the page's path and the counts. A finding that was not in the
previous result.json is marked new.
A plain run holds one cpu slot of the machine lock's maint-lint kind (kitlib.maintlint.slot) for
the whole run, waiting at most the kind's warn_min; --dry-run takes no slot.
Exit codes: 0 no finding, 1 findings, 2 USAGE (--dry-run without --out, or no --result and no
pm_data.path to put it under), 3 SKIPPED (the lock stayed busy: nothing ran, and result.json and
the page are left as they were), else the lock's own exit code when it refuses the run otherwise.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import maintlint                                       # noqa: E402
from kitlib.maintlint import page, result as rmod, slot            # noqa: E402


def _counts(res):
    out = ['%-20s %d' % (c, n) for c, n in res['counts'].items() if n]
    led = res.get('ledger')
    out.append('ledger: %s' % ('NOT CHECKED' if led is None else
                               'stale_cites %(stale_cites)d, superseded_live %(superseded_live)d'
                               ' of %(superseded)d' % led))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='The maintenance lint of one repository (read-only).')
    ap.add_argument('-C', dest='root', default='.', help='the checkout to lint')
    ap.add_argument('--dry-run', action='store_true',
                    help='write only the page, to --out; never result.json')
    ap.add_argument('--result', help='the result.json to read and write')
    ap.add_argument('--out', help='the page HTML to write (default page.html beside the result;'
                                  ' required with --dry-run)')
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        print('USAGE: -C %s is not a folder' % a.root, file=sys.stderr)
        return 2
    if a.dry_run and not a.out:
        print('USAGE: --dry-run writes only the page, so it needs --out FILE', file=sys.stderr)
        return 2
    path = a.result
    if not path:
        folder, why = rmod.default_dir(maintlint.repo_name(root))
        if folder is None and not a.dry_run:
            print('USAGE: no --result, and no pm_data folder to put it in: %s' % why,
                  file=sys.stderr)
            return 2
        path = os.path.join(folder, rmod.NAME) if folder else None
    if a.dry_run:
        return _report(_lint(root, path, a.out, False))
    try:
        held = slot.take(maintlint.repo_name(root))
    except slot.Skipped as e:
        print(e, flush=True)
        return slot.EXIT_SKIPPED
    except slot.Refused as e:
        print(e, file=sys.stderr)
        return e.code
    with held:
        res = _lint(root, path, a.out, True)
    return _report(res)


def _lint(root, path, out, write):
    """Runs the lint, writes result.json (when `write`) and the page, and prints their paths."""
    res = maintlint.run(root, previous=rmod.read(path) if path else None)
    if write:
        print('result: %s' % rmod.write(path, res))
    out = os.path.abspath(out or os.path.join(os.path.dirname(os.path.abspath(path)), rmod.PAGE))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(page.render(res))
    print('page: %s' % out)
    return res


def _report(res):
    for note in res['notes']:
        print('note: %s' % note)
    for line in _counts(res):
        print(line)
    print('%d finding(s) in %.2f s' % (len(res['findings']), res['elapsed_s']), flush=True)
    return 1 if res['findings'] else 0


if __name__ == '__main__':
    sys.exit(main())
