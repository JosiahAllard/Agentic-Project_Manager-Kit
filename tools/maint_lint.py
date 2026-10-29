"""maint_lint.py: the maintenance lint of one repository (docs/maint-lint.md). It writes only the
gate-time record, in a plain run.

  maint_lint.py [-C DIR] [--dry-run] [--result FILE] [--out FILE]

Runs every check of kitlib.maintlint on the checkout DIR: keyword holds, unpriced records, dead
pulls, dead blockers, duplicates, parked records whose hub question is answered, the backlog
audit (7 days, no fetch) as counts, body, files and scope paths, the worktrees the nightly sweep
leaves, the decision ledger as counts, and each gate step's median time over full gates against
"maint_lint".gate_time.bound_s (kitlib.maintlint.gatetime), and, in a repository with a module
map, one count line summing the gates' scope records (kitlib.modulemap.scopelog). No check
flags anything for its age alone. Every finding prints the command that fixes it; nothing is run, and no ledger entry,
worktree or git ref is written. The one write: a plain run files a backlog record for each step
over the bound, when "maint_lint".gate_time.prefix is set and no live record of that step is in
the store (one `gate time:` line each); --dry-run files nothing.

Without --dry-run it writes the result (default `<pm_data>/maint-lint/<repo>/result.json`, beside
the backlog's audit/ folder) and the page (--out, default page.html beside the result), and prints
the two paths and the counts. With --dry-run (--out is then required) it writes only the page to
--out, never result.json, and prints the page's path and the counts. A finding that was not in the
previous result.json is marked new. A plain run then publishes the page on the Action Hub as
`maint-lint-<repo>` (kitlib.maintlint.publish) and prints `PUBLISHED <id>`, or a NOT CHECKED note
when the hub cannot take it; that changes no exit code. --dry-run never publishes.
A plain run holds one cpu slot of the machine lock's maint-lint kind (kitlib.maintlint.slot) for
the whole run, waiting at most 15 min; --dry-run takes no slot. A skipped run writes the skip
marker skipped.json beside result.json (its reason and time); a run that writes result.json removes
it.
Exit codes: 0 no finding, 1 findings, 2 USAGE (--dry-run without --out, or no --result and no
pm_data.path to put it under), 3 SKIPPED (the lock stayed busy, or a strong hold reserves the
machine: nothing ran, and result.json and the page are left as they were), 4 the lock refused with
its own exit 1 or 2, 5 ERROR (an unexpected error in the lock or the lint: one `ERROR: <type>:
<message>` line on stderr), else the lock's own exit code when it refuses the run otherwise.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import maintlint                                       # noqa: E402
from kitlib.maintlint import gatetime, page, publish, result as rmod, slot   # noqa: E402
from kitlib.modulemap import scopelog                              # noqa: E402

# An unexpected error (any Exception other than the lock's refusals): none of 0-4, nor a lock code.
EXIT_ERROR = 5


def _counts(res):
    out = ['%-20s %d' % (c, n) for c, n in res['counts'].items() if n]
    led = res.get('ledger')
    out.append('ledger: %s' % ('NOT CHECKED' if led is None else
                               'stale_cites %d, rules %d in %d file(s), over budget %d, '
                               'conflict pairs %d, overrides %d'
                               % (led['stale_cites'], sum(led['rules'].values()),
                                  len(led['rules']), len(led['over_budget']),
                                  led['conflict_pairs'], led['overrides'])))
    if res.get('module_scope') is not None:
        out.append(scopelog.line(res['module_scope']))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='The maintenance lint of one repository (it writes '
                                             'only the gate-time record, in a plain run).')
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
    try:
        return _run(root, path, a)
    except Exception as e:
        # Python's own exit 1 for a traceback would read as findings. BaseException (Ctrl+C,
        # SystemExit, a deliberate BaseException) is not caught.
        print('ERROR: %s: %s' % (type(e).__name__, str(e).replace('\n', ' ')), file=sys.stderr)
        return EXIT_ERROR


def _run(root, path, a):
    """The lint after its arguments were checked: --dry-run, or inside the held slot."""
    if a.dry_run:
        return _report(_lint(root, path, a.out, False)[0])
    try:
        held = slot.take(maintlint.repo_name(root))
    except slot.Skipped as e:
        print(e, flush=True)
        try:
            slot.write_skip(path, e)
        except OSError as err:
            print('note: the skip marker was not written: %s' % err, file=sys.stderr)
        return slot.EXIT_SKIPPED
    except slot.Refused as e:
        print(e, file=sys.stderr)
        return e.code
    with held:
        res, html = _lint(root, path, a.out, True)
    line, notes = publish.publish(res, html)
    if line:
        print(line)
    # The one write, outside the slot like the page: a record per gate step over its bound.
    for said in gatetime.file(root, res):
        print(said)
    return _report(dict(res, notes=res['notes'] + notes))


def _lint(root, path, out, write):
    """Runs the lint, writes result.json (when `write`) and the page, and prints their paths.
    Returns the result and the page's html."""
    res = maintlint.run(root, previous=rmod.read(path) if path else None)
    if write:
        print('result: %s' % rmod.write(path, res))
        try:
            slot.clear_skip(path)
        except OSError as err:
            print('note: the old skip marker was not removed: %s' % err, file=sys.stderr)
    out = os.path.abspath(out or os.path.join(os.path.dirname(os.path.abspath(path)), rmod.PAGE))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    html = page.render(res)
    with open(out, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(html)
    print('page: %s' % out)
    return res, html


def _report(res):
    for note in res['notes']:
        print('note: %s' % note)
    for line in _counts(res):
        print(line)
    print('%d finding(s) in %.2f s' % (len(res['findings']), res['elapsed_s']), flush=True)
    return 1 if res['findings'] else 0


if __name__ == '__main__':
    sys.exit(main())
