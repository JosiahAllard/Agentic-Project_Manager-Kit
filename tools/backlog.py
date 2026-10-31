"""backlog.py: the backlog store's reads and writes (docs/backlog.md).

  backlog.py [-C DIR] [--session S] [--as BRANCH] [--no-fetch] <verb> ...

  reads   next [--cap N | --all] [--with ID]...     show ID     check     landable BRANCH...
          list [--blocked [--on M]] [--needs-priority] [--status S] [--area A]
          stale [--days N]       find TEXT      (open and closed records: id, summary, body)
  writes  add ID --summary S --scope P... --found-on BRANCH|none --why-not-folded T
              [--uses NAME]... [--effort s|m|l] [--ref R] [--reopen] [--body-file F]
              (S: at most 120 characters)
          set ID [--<field> V]... [--unset K]... [--body-file F]
          block ID --on M... [--reason T] [--owner-ask Q]   (adds; unblock removes)
              [--until C]   (one --on: the condition that ends it; docs/backlog.md "block --until")
          retarget OLD NEW [--apply]                (a moved path; a dry run without --apply)
          uncite [--apply]       (owner ruling and question ids -> words; a dry run without --apply)
          unblock ID [--on M] [--reason T]      (several blockers: --on and --reason)
          pull ID [--reason T] [--blocker ID]   release ID --reason T
          close ID --reason T                   retire ID --why T
          delete ID --why T                     clear-review ID --reason T
          set-status ID parked [--q Q-nnn]      unpark ID
  repair  catch-up [--apply PATH...]     (the PM data worktree's uncommitted record paths; a dry
                                          run lists them, --apply commits the paths it names)

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.backlog, prints and exits.
Every write first meets the session's role (kitlib.backlog.policy: the session mark, unmarked is
the owner, against kitlib/backlog/roles.py; docs/backlog.md "Roles"): a row it may not take is
REFUSED before the write plans. A published write of a MEDIUM or HIGH row appends its line to the
audit log, <pm_data>/audit/backlog.jsonl (kitlib.backlog.audit; docs/backlog.md "Audit log"); no
pm_data is a NOTE and a log that cannot be written a WARNING, on standard error, after the write.
A marked session may not give `--session` a by-design writer's name (gate-land, owner-hub,
daily-full, hub-condition: audit_check.BY_DESIGN): REFUSED before the store is opened.
Exit codes: 0 OK (a `show` of a closed, retired or deleted id too: ENDED), 1 NOT FOUND (an id
no commit removed) / ERROR / a red check / a landable that cannot tell, 2 USAGE / REFUSED /
NOT LANDABLE.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import kitroot, sessionmark                            # noqa: E402
from kitlib.backlog import audit                                    # noqa: E402
from kitlib.backlog import audit_check                              # noqa: E402
from kitlib.backlog import check as checkmod                        # noqa: E402
from kitlib.backlog import find as findmod                          # noqa: E402
from kitlib.backlog import landable as landmod                      # noqa: E402
from kitlib.backlog import offer as omod                            # noqa: E402
from kitlib.backlog import plan as pmod                             # noqa: E402
from kitlib.backlog import policy                                   # noqa: E402
from kitlib.backlog import record as rmod                           # noqa: E402
from kitlib.backlog import stale as stalemod                        # noqa: E402
from kitlib.backlog import store as smod                            # noqa: E402
from kitlib.backlog.errors import Failure, IndexStale, Refusal, Usage  # noqa: E402
from kitlib.backlog.opener import (FIELD_FLAGS, WRITES, open_store, planner,  # noqa: E402
                                   retarget_dry, uncite_dry)
from kitlib.coordstate import log as coordlog                       # noqa: E402


def say(line=''):
    print(line, flush=True)


def audit_line(verb, rid, rows, tip, session):
    """After a published write: its audit line (kitlib.backlog.audit), and on standard error the
    note or warning when there is none. Never a refusal: the write stands, so any failure here,
    one after_write does not catch too, is a warning and the exit code is the write's."""
    try:
        said = audit.after_write(verb, rid, rows, tip, session)
    except Exception as exc:                    # noqa: BLE001 - the write is published; say, never fail it
        said = (audit.WARNING, 'the write stands, but its audit line was not written: %s: %s'
                % (type(exc).__name__, ' '.join(str(exc).split())))
    if said:
        sys.stderr.write('%s: %s\n' % said)
        sys.stderr.flush()


# One correct command per verb, printed under a usage error. Each one parses and passes the verb's
# own checks: tests/backlog/test_usage_examples.py.
EXAMPLES = {
    'next': 'backlog.py next --cap 5',
    'show': 'backlog.py show EX-EXAMPLE',
    'find': 'backlog.py find "text to look for"',
    'list': 'backlog.py list --status open',
    'check': 'backlog.py check',
    'stale': 'backlog.py stale --days 30',
    'landable': 'backlog.py landable kit/example-branch',
    'add': ('backlog.py add EX-EXAMPLE --summary "<at most 120 characters>" --scope <path> '
            '--found-on none --why-not-folded "<why>" --body-file <file>'),
    'set': 'backlog.py set EX-EXAMPLE --scope <path> --body-file <file>',
    'block': 'backlog.py block EX-EXAMPLE --on owner --owner-ask "item:EX-OTHER until <what the owner decides>"',
    'retarget': 'backlog.py retarget <old-path> <new-path>',
    'uncite': 'backlog.py uncite',
    'unblock': 'backlog.py unblock EX-EXAMPLE --on owner --reason "<why it ends>"',
    'pull': 'backlog.py --as <branch> pull EX-EXAMPLE',
    'release': 'backlog.py --as <branch> release EX-EXAMPLE --reason "<why>"',
    'close': 'backlog.py close EX-EXAMPLE --reason "<what landed>"',
    'retire': 'backlog.py retire EX-EXAMPLE --why "<why>"',
    'delete': 'backlog.py delete EX-EXAMPLE --why "<why>"',
    'clear-review': 'backlog.py clear-review EX-EXAMPLE --reason "<why>"',
    'set-status': 'backlog.py set-status EX-EXAMPLE parked --q <Q-id>',
    'unpark': 'backlog.py unpark EX-EXAMPLE',
    'catch-up': 'backlog.py catch-up',
}


def example_line(verb):
    """`  example: <command>` for a verb that has one, else ''."""
    return '  example: %s' % EXAMPLES[verb] if verb in EXAMPLES else ''


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2, then one example of the verb."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        words = self.prog.split()
        if len(words) > 1 and example_line(words[-1]):
            sys.stderr.write(example_line(words[-1]) + '\n')
        sys.exit(2)


def build_parser():
    p = Parser(prog='backlog.py', description='The backlog store (docs/backlog.md).')
    p.add_argument('-C', dest='root', default=None, help='run against this worktree')
    p.add_argument('--session', default=None, help='the session (commit message, `next` offer)')
    p.add_argument('--as', dest='branch', default=None, help='the branch to pull or release as')
    p.add_argument('--no-fetch', action='store_true', help='read the local copy of the store')
    sub = p.add_subparsers(dest='verb', parser_class=Parser)
    sub.required = True

    v = sub.add_parser('next')
    v.add_argument('--cap', type=int, default=None)
    v.add_argument('--all', action='store_true', help='every startable item, no cap')
    v.add_argument('--with', dest='seeds', action='append', default=[])
    v = sub.add_parser('show')
    v.add_argument('id')
    v = sub.add_parser('find')
    v.add_argument('text', help='looked for in each id, summary and body, any case')
    v = sub.add_parser('list')
    v.add_argument('--blocked', action='store_true')
    v.add_argument('--on', default=None)
    v.add_argument('--needs-priority', action='store_true')
    v.add_argument('--status', default=None, choices=rmod.STATES)
    v.add_argument('--area', default=None)
    sub.add_parser('check')
    v = sub.add_parser('stale')
    v.add_argument('--days', type=int, default=stalemod.DEFAULT_DAYS)
    v = sub.add_parser('landable')
    v.add_argument('branches', nargs='+')

    v = sub.add_parser('add')
    v.add_argument('id')
    v.add_argument('--summary', required=True)
    v.add_argument('--scope', action='append', default=[])
    v.add_argument('--uses', action='append', default=[],
                   help='a resource .kit.json declares that the record needs while pulled')
    v.add_argument('--effort', default=None)
    v.add_argument('--ref', default=None)
    v.add_argument('--reopen', action='store_true')
    v.add_argument('--found-on', dest='found_on', required=True,
                   help='the branch the finding was made on, or none')
    v.add_argument('--why-not-folded', dest='why_not_folded', required=True,
                   help='why the finding is not fixed on that branch')
    v.add_argument('--body-file', default=None, help='the body: what the summary leaves out')
    v = sub.add_parser('set')
    v.add_argument('id')
    for key in FIELD_FLAGS:
        flags = sorted(set(['--' + key, '--' + key.replace('_', '-')]))
        v.add_argument(*flags, dest='f_' + key, action='append')
    v.add_argument('--unset', action='append', default=[])
    v.add_argument('--body-file', default=None)
    v = sub.add_parser('block')
    v.add_argument('id')
    v.add_argument('--on', action='append', default=[])
    v.add_argument('--reason', default=None)
    v.add_argument('--owner-ask', default=None)
    v.add_argument('--until', default=None)
    v = sub.add_parser('retarget')
    v.add_argument('old')
    v.add_argument('new')
    v.add_argument('--apply', action='store_true', help='write; without it a dry run')
    v = sub.add_parser('uncite')
    v.add_argument('--apply', action='store_true',
                   help='write; without it a dry run that lists the wording to check')
    v = sub.add_parser('unblock')
    v.add_argument('id')
    v.add_argument('--on', default=None)
    v.add_argument('--reason', default=None)
    v = sub.add_parser('pull')
    v.add_argument('id')
    v.add_argument('--reason', default=None)
    v.add_argument('--blocker', default=None)
    v = sub.add_parser('release')
    v.add_argument('id')
    v.add_argument('--reason', required=True)
    v = sub.add_parser('close')
    v.add_argument('id')
    v.add_argument('--reason', required=True)
    v = sub.add_parser('retire')
    v.add_argument('id')
    v.add_argument('--why', required=True)
    v = sub.add_parser('delete')
    v.add_argument('id')
    v.add_argument('--why', required=True)
    v = sub.add_parser('clear-review')
    v.add_argument('id')
    v.add_argument('--reason', required=True)
    v = sub.add_parser('set-status')
    v.add_argument('id')
    v.add_argument('status')
    v.add_argument('--q', default=None, help='the owner question it waits on, if one')
    v = sub.add_parser('unpark')
    v.add_argument('id')
    v = sub.add_parser('catch-up')
    v.add_argument('--apply', nargs='+', default=None, metavar='PATH',
                   help='commit these record paths, each named; without it a dry run')
    return p


# --- reads ----------------------------------------------------------------------------------------

def banner(snap):
    b = omod.Banner(snap)
    for line in b.lines():
        say(line)
    return b


def row(rec, ranks):
    return '%4s  %-28s p%-2s %-7s %-10s %s' % (
        omod.rank_label(rec, ranks), rec.id, rec.get('priority') or '?', rec.get('state') or '?',
        rec.get('area') or '?', rec.get('summary') or '')


def do_next(args, ctx, snap):
    if args.all and args.cap is not None:
        raise Usage('`--all` lists every startable item; it takes no `--cap`')
    if args.cap is not None and args.cap < 1:
        raise Usage('`--cap %d`: the cap is a whole number >= 1' % args.cap)
    # The repository's cap (`.kit.json` backlog.cap, plan.Context.cap): the one `pull` checks.
    cap = None if args.all else (args.cap if args.cap is not None else ctx.cap)
    b = banner(snap)
    warn = b.queue_warning()
    if warn:
        say(warn)
    holding, dead, why = omod.in_flight_split(snap, ctx.root, ctx.remote)
    if why:
        say('in flight: cannot read the branches (%s); every pull holds its scope' % why)
    for rec in dead:
        # The record's own session takes a dead pull over with a plain `pull` (plan_pull);
        # anyone else releases it first.
        taker = ('a pull by session %s takes it over; otherwise ' % rec.get('session')
                 if rec.get('session') else '')
        say('DEAD PULL %s (pulled_by %s has no branch): %s`release %s --reason ...`'
            % (rec.id, rec.get('pulled_by'), taker, rec.id))
    # The offer `pull` checks against (plan.offer_for): the branch's own pulls take no scope.
    offers, passed, _holding, own = pmod.offer_for(snap, ctx, cap=cap, seeds=args.seeds,
                                                   holding=holding)
    mine = [r.id for r in holding if own and r.get('pulled_by') == own]
    if mine:
        say('as branch %s: its own pulls (%s) take no scope here, so a record overlapping only '
            'them is offered to bundle onto it' % (own, ', '.join(mine)))
    ranks = omod.ranked(snap)
    say('')
    say('NEXT (%d offered%s%s)' % (len(offers), ', session %s' % ctx.session if ctx.session else '',
                                   ', --all: no cap' if cap is None else ''))
    for o in offers:
        say(row(o.rec, ranks) + ('  (--with)' if o.seeded else ''))
    for p in passed:
        say('  passed over %s (rank %s): %s' % (p.rec.id, p.rank, p.why))
    summary = omod.blocker_summary(passed)
    if summary:
        say('  blockers, most first:')
        for line in summary:
            say('    ' + line)
    return 0


def do_show(args, ctx, snap):
    rec = pmod.require_shown(snap, ctx, args.id)
    ranks = omod.ranked(snap)
    bucket, _ = omod.classify(rec)
    say('%s  rank %s  %s  (%s)' % (rec.id, omod.rank_label(rec, ranks), bucket, rec.path))
    if rmod.is_owner_held(rec):
        say('OWNER HOLD (%s): %s' % (rmod.owner_hold_label(rec), rec.get('owner_ask')))
    uses = pmod.uses_shown(snap, ctx, rec)
    if uses:
        say(uses)
    say('')
    say((rec.text or '').rstrip('\n'))
    return 0


def do_find(args, ctx, snap):
    if not args.text.strip():
        raise Usage('`find` needs some text to look for')
    banner(snap)
    lines, omitted = findmod.search(snap, ctx.store, args.text)
    if not lines:
        say('no record matches "%s" (open or closed)' % args.text.strip())
        return 0
    for line in lines:
        say(line)
    say('%d hit(s) shown%s' % (len(lines), ', %d more left out (narrow the text)' % omitted
                               if omitted else ''))
    return 0


def do_list(args, ctx, snap):
    banner(snap)
    ranks = omod.ranked(snap)
    recs = snap.readable
    if args.area:
        recs = [r for r in recs if r.get('area') == args.area]
    if args.needs_priority:
        # A record marked for review at `-` still wants its price (the pricing pass).
        recs = sorted((r for r in recs if omod.classify(r)[0] == omod.UNPRIORITISED_OPEN or
                       omod.classify(r)[0] == omod.REVIEW and r.get('priority') == '-'),
                      key=lambda r: r.id)
    elif args.blocked:
        recs = [r for r in recs if omod.classify(r)[0] == omod.HELD]
        if args.on:
            recs = [r for r in recs if args.on in r.all('blocked_on')]
        recs = sorted(recs, key=lambda r: (', '.join(r.all('blocked_on')), r.id))
    elif args.on:
        raise Usage('`--on` filters `--blocked`')
    if args.status:
        recs = [r for r in recs if r.get('state') == args.status]
    else:
        recs = sorted(recs, key=lambda r: (str(ranks.get(r.id, 10 ** 6)).zfill(7), r.id))
    say('')
    for r in recs:
        line = row(r, ranks)
        if args.blocked:
            line += '  [%s]' % ', '.join(r.all('blocked_on'))
        if r.get('state') == 'parked':
            days = rmod.parked_days(r, ctx.date())
            age = '?' if days is None else '%dd' % days
            line += '  [q %s, parked %s]' % (r.get('q') or '?', age)
        if r.has('review'):
            line += '  [review %s]' % r.get('review')
        say(line)
    say('%d record(s)' % len(recs))
    return 0


def do_stale(args, ctx, snap):
    if args.days < 1:
        raise Usage('`--days` is at least 1')
    banner(snap)
    for line in stalemod.sweep(snap, ctx.store.touched(snap), ctx.date(), args.days,
                               ctx.root).lines():
        say(line)
    return 0


def do_check(args, ctx, snap):
    say('CHECK %s (%s)' % (snap.head_label(), snap.where))
    red = False
    for res in checkmod.check_store(snap, implicit=getattr(ctx.store.backend, 'implicit', False),
                                    resources=ctx.capacity(snap)):
        say(res.line())
        for f in res.findings:
            say('    FAIL %s:%s %s' % (f.path, f.line, f.message))
        for f in res.warnings:
            say('    WARN %s:%s %s' % (f.path, f.line, f.message))
        red = red or res.red
    say('RED' if red else 'GREEN')
    return 1 if red else 0


def do_landable(args, ctx, snap, backend):
    verdicts = landmod.check(backend, snap, ctx.root, args.branches, base=landmod.code_base(ctx))
    for v in verdicts:
        for line in landmod.lines(v, kitroot.command('backlog')):
            say(line)
    return landmod.exit_code(verdicts)


# --- writes ---------------------------------------------------------------------------------------

def index_stale(exc):
    """The WARNING line for a write that is committed with the worktree's index left stale
    (errors.IndexStale): the write stands, so the verb still reports it and exits 0."""
    say('WARNING: %s' % ' '.join(str(exc).split()))


def do_write(args, ctx, store):
    made, rows = [], []

    def plan(snap):                              # the change, kept for an IndexStale report
        rows[:] = policy.check(args, ctx, snap)  # the session's role may take it (roles.py)
        made[:] = [planner(args, ctx)(snap)]
        return made[0]
    stale = None
    try:
        change, tip = store.write(plan)
    except IndexStale as exc:                    # committed: report it as written
        stale, change, tip = exc, made[0], exc.commit
    for line in change.lines:
        say(line)
    if not change.writes:
        say('nothing changed; nothing was written')
        return 0
    if stale is not None and not tip:
        say('WROTE %s -> HEAD already held it' % store.backend.describe())
    else:
        say('WROTE %s -> %s' % (store.backend.describe(), tip[:10] if tip else 'the worktree'))
    say('  %s' % change.subject)
    if stale is not None:
        index_stale(stale)
    # the row a plan found (a collision with a live pull, plan._collision_change) is audited too
    rows.extend(r for r in getattr(change, 'rows', ()) if r not in rows)
    audit_line(args.verb, getattr(args, 'id', None), rows, tip, args.session)
    coordlog.note_backlog(args.verb, getattr(args, 'id', None), getattr(args, 'branch', None))
    return 0


def do_dry(change):
    """`retarget` or `uncite` without `--apply`: prints the change it would write
    (kitlib/backlog/opener.py `retarget_dry`, `uncite_dry`); nothing is published."""
    for line in change.lines:
        say(line)
    say('DRY RUN: nothing was written%s' % ('; `--apply` writes it as one store commit'
                                            if change.writes else ''))
    return 0


def do_catch_up(args, store):
    """`catch-up`: each uncommitted path in the folder store as `<kind>  <path>`, then the counts;
    with `--apply PATH...` commit the named record paths (kitlib.backlog.store.catch_up)."""
    backend = store.backend
    if args.apply is not None:
        stale = None
        try:
            tip = smod.catch_up(backend, args.apply, store.session)
        except IndexStale as exc:                # committed: report it as caught up
            stale, tip = exc, exc.commit
        say('CAUGHT UP %d record path(s) in %s -> %s'
            % (len(set(args.apply)), backend.describe(), tip[:10] if tip else 'nothing to commit'))
        if stale is not None:
            index_stale(stale)
        return 0
    if not isinstance(backend, smod.MainBackend):
        raise Usage('`catch-up` is for a folder store; %s commits its own writes'
                    % backend.describe())
    found = backend.pending()
    say('UNCOMMITTED in %s (%s)' % (backend.describe(), 'the PM data worktree: its writes commit'
                              if backend.commit_branch() else 'not the PM data worktree'))
    for kind, path in found:
        say('  %-12s %s' % (kind, path))
    counts = {}
    for kind, _ in found:
        counts[kind] = counts.get(kind, 0) + 1
    say('%d path(s)%s' % (len(found), ''.join(', %d %s' % (counts[k], k) for k in sorted(counts))))
    records = [p for kind, p in found if kind != 'not a record']
    if records:
        say('DRY RUN: nothing was committed; commit the record paths with: catch-up --apply %s'
            % smod.quoted(records))
    else:
        say('DRY RUN: no uncommitted record path')
    return 0


def refuse_by_design_session(session, environ=None):
    """A marked session (kitlib.sessionmark) may not run as one of the sessions whose store writes
    add no audit line by design (audit_check.BY_DESIGN; docs/backlog.md "Audit log"): Refusal.
    The owner's own unmarked shell may."""
    name = (session or '').strip().lower()
    if name in audit_check.BY_DESIGN and sessionmark.mark(environ) is not None:
        raise Refusal('`--session %s` is the name of a kit writer whose store writes add no audit '
                      'line by design (%s); a marked session may not use it: give your own '
                      'session name' % (session, ', '.join(audit_check.BY_DESIGN)))


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.verb == 'block':
            err = rmod.block_citation_error(args.on, args.reason, args.owner_ask)
            if err:
                raise Usage(err)
        refuse_by_design_session(args.session)
        root = os.path.abspath(args.root or os.getcwd())
        ctx = open_store(root, fetch=not args.no_fetch, session=args.session, branch=args.branch)
        store = ctx.store
        if args.verb in WRITES or (args.verb in ('retarget', 'uncite') and args.apply):
            return do_write(args, ctx, store)
        if args.verb == 'retarget':
            return do_dry(retarget_dry(args, ctx))
        if args.verb == 'uncite':
            return do_dry(uncite_dry(ctx))
        if args.verb == 'catch-up':
            if args.apply is not None:
                policy.check(args, ctx, None)    # the commit takes the `catch-up` row (roles.py)
            return do_catch_up(args, store)
        if args.verb == 'check':
            snap, note = checkmod.load_for_check(store)
            if note:
                say('NOTE: %s' % note)
        else:
            snap = store.load()
        if args.verb == 'landable':
            return do_landable(args, ctx, snap, store.backend)
        return {'next': do_next, 'show': do_show, 'find': do_find, 'list': do_list,
                'check': do_check, 'stale': do_stale}[args.verb](args, ctx, snap)
    except omod.PartitionError as exc:
        say('ERROR: the partition does not add up: %s' % exc)
        return 1
    except Refusal as exc:
        say('%s: %s' % (exc.label, exc))
        if isinstance(exc, Usage) and example_line(args.verb):
            say(example_line(args.verb))
        return exc.code


if __name__ == '__main__':
    sys.exit(main())
