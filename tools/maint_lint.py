"""maint_lint.py: the maintenance lint of one repository, read-only (docs/maint-lint.md).

  maint_lint.py [-C DIR] [--dry-run] [--result FILE] [--out FILE]

Runs every check of kitlib.maintlint on the checkout DIR: keyword holds, unpriced records, dead
pulls, dead blockers, duplicates, the backlog audit (7 days, no fetch), body, files and scope
paths, worktrees by class, and the decision ledger as counts. No check flags anything for its age
alone. Every finding prints the command that fixes it; nothing is run, and no record, ledger,
worktree or git ref is written.

Without --dry-run it writes the result (default `<kit temp>/<repo>-maint-lint/result.json`) and the
page (default page.html beside it), and prints the two paths and the counts. With --dry-run it
writes nothing and prints each finding with its fix, the counts and the time taken. A finding that
was not in the previous result.json is marked new.
Exit codes: 0 no finding, 1 findings, 2 USAGE.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import maintlint                                       # noqa: E402
from kitlib.maintlint import page, result as rmod                  # noqa: E402


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
    ap.add_argument('--dry-run', action='store_true', help='print the findings; write nothing')
    ap.add_argument('--result', help='the result.json to read and write')
    ap.add_argument('--out', help='the page HTML to write (default page.html beside the result)')
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        print('USAGE: -C %s is not a folder' % a.root, file=sys.stderr)
        return 2
    path = a.result or os.path.join(
        rmod.default_dir(root, os.path.basename(root.rstrip('\\/'))), rmod.NAME)
    res = maintlint.run(root, previous=rmod.read(path))
    if a.dry_run:
        for f in res['findings']:
            print('%s%s: %s' % ('NEW ' if f['new'] else '', f['check'], f['text']))
            print('    fix: %s' % f['fix'])
    else:
        print('result: %s' % rmod.write(path, res))
        out = a.out or os.path.join(os.path.dirname(os.path.abspath(path)), rmod.PAGE)
        with open(out, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(page.render(res))
        print('page: %s' % os.path.abspath(out))
    for note in res['notes']:
        print('note: %s' % note)
    for line in _counts(res):
        print(line)
    print('%d finding(s) in %.2f s' % (len(res['findings']), res['elapsed_s']), flush=True)
    return 1 if res['findings'] else 0


if __name__ == '__main__':
    sys.exit(main())
