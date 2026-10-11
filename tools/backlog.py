"""backlog.py: the backlog store's reads and writes (docs/backlog.md).

  backlog.py [-C DIR] [--session S] [--as BRANCH] [--no-fetch] <verb> ...

  reads   next [--cap N | --all] [--with ID]...     show ID     check     landable BRANCH...
          list [--blocked [--on M]] [--needs-priority] [--status S] [--area A]
          stale [--days N]
  writes  add ID --summary S --scope P... [--effort s|m|l] [--ref R] [--reopen]
          set ID [--<field> V]... [--unset K]... [--ruling R] [--body-file F] [--hold-ruling R]
          block ID --on M... [--reason T] [--owner-ask Q] [--ruling R]
          retarget OLD NEW [--apply]                (a moved path; a dry run without --apply)
          unblock ID [--on M] [--reason T] [--ruling R]      (several blockers: --on and --reason)
          pull ID [--reason T] [--ruling R] [--blocker ID]   release ID --reason T
          close ID --reason T [--ruling R]                   retire ID --why T [--ruling R]
          set-status ID parked --q Q-nnn                     unpark ID --ruling R
  start   migrate [--dry-run] [--to ref|branch] [--dir D]   (main's records -> a new store)

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.backlog, prints and exits.
Exit codes: 0 OK, 1 NOT FOUND / ERROR / a red check / a landable that cannot tell, 2 USAGE /
REFUSED / NOT LANDABLE.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.backlog import check as checkmod                        # noqa: E402
from kitlib.backlog import landable as landmod                      # noqa: E402
from kitlib.backlog import migrate as mmod                          # noqa: E402
from kitlib.backlog import offer as omod                            # noqa: E402
from kitlib.backlog import plan as pmod                             # noqa: E402
from kitlib.backlog import record as rmod                           # noqa: E402
from kitlib.backlog import retarget as retmod                       # noqa: E402
from kitlib.backlog import stale as stalemod                        # noqa: E402
from kitlib.backlog import store as smod                            # noqa: E402
from kitlib.backlog.errors import Failure, Refusal, Usage           # noqa: E402
from kitlib.config import ConfigError, load as load_config          # noqa: E402
from kitlib.git import repo as gitrepo                              # noqa: E402

# `set --<field>` for every field but hold_ruling, whose `--hold-ruling` is the recite.
FIELD_FLAGS = [k for k in rmod.FIELDS if k != 'hold_ruling']
WRITES = ('add', 'set', 'block', 'unblock', 'pull', 'release', 'close', 'retire', 'set-status',
          'unpark')


def say(line=''):
    print(line, flush=True)


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
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
    v.add_argument('--effort', default=None)
    v.add_argument('--ref', default=None)
    v.add_argument('--reopen', action='store_true')
    v = sub.add_parser('set')
    v.add_argument('id')
    for key in FIELD_FLAGS:
        flags = sorted(set(['--' + key, '--' + key.replace('_', '-')]))
        v.add_argument(*flags, dest='f_' + key, action='append')
    v.add_argument('--unset', action='append', default=[])
    v.add_argument('--ruling', default=None)
    v.add_argument('--body-file', default=None)
    v.add_argument('--hold-ruling', default=None)
    v = sub.add_parser('block')
    v.add_argument('id')
    v.add_argument('--on', action='append', default=[])
    v.add_argument('--reason', default=None)
    v.add_argument('--owner-ask', default=None)
    v.add_argument('--ruling', default=None)
    v = sub.add_parser('retarget')
    v.add_argument('old')
    v.add_argument('new')
    v.add_argument('--apply', action='store_true', help='write; without it a dry run')
    v = sub.add_parser('unblock')
    v.add_argument('id')
    v.add_argument('--on', default=None)
    v.add_argument('--reason', default=None)
    v.add_argument('--ruling', default=None)
    v = sub.add_parser('pull')
    v.add_argument('id')
    v.add_argument('--reason', default=None)
    v.add_argument('--ruling', default=None)
    v.add_argument('--blocker', default=None)
    v = sub.add_parser('release')
    v.add_argument('id')
    v.add_argument('--reason', required=True)
    v = sub.add_parser('close')
    v.add_argument('id')
    v.add_argument('--reason', required=True)
    v.add_argument('--ruling', default=None)
    v = sub.add_parser('retire')
    v.add_argument('id')
    v.add_argument('--why', required=True)
    v.add_argument('--ruling', default=None)
    v.add_argument('--owner-said', default=None)           # refused: kept so the message names --ruling
    v = sub.add_parser('set-status')
    v.add_argument('id')
    v.add_argument('status')
    v.add_argument('--q', required=True)
    v = sub.add_parser('unpark')
    v.add_argument('id')
    v.add_argument('--ruling', required=True)
    v = sub.add_parser('migrate')
    v.add_argument('--dry-run', action='store_true')
    v.add_argument('--to', default=None, choices=('ref', 'branch'))
    v.add_argument('--dir', default=None)
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
    cap = None if args.all else (args.cap if args.cap is not None else omod.DEFAULT_CAP)
    b = banner(snap)
    warn = b.queue_warning()
    if warn:
        say(warn)
    holding, dead, why = omod.in_flight_split(snap, ctx.root, ctx.remote)
    if why:
        say('in flight: cannot read the branches (%s); every pull holds its scope' % why)
    for rec in dead:
        say('DEAD PULL %s (pulled_by %s has no branch): `release %s --reason "..."`'
            % (rec.id, rec.get('pulled_by'), rec.id))
    result, refusals, ranks = omod.next_offer(snap, holding, cap=cap, seeds=args.seeds,
                                              session=ctx.session, unmet=ctx.unmet(snap))
    if refusals:
        raise Failure('cannot compute an offer: %s' % '; '.join(refusals))
    offers, passed = result
    say('')
    say('NEXT (%d offered%s%s)' % (len(offers), ', session %s' % ctx.session if ctx.session else '',
                                   ', --all: no cap' if cap is None else ''))
    for o in offers:
        say(row(o.rec, ranks) + ('  (--with)' if o.seeded else ''))
    for p in passed:
        say('  passed over %s (rank %s): %s' % (p.rec.id, p.rank, p.why))
    return 0


def do_show(args, ctx, snap):
    rec = pmod.require_shown(snap, ctx, args.id)
    ranks = omod.ranked(snap)
    bucket, _ = omod.classify(rec)
    say('%s  rank %s  %s  (%s)' % (rec.id, omod.rank_label(rec, ranks), bucket, rec.path))
    if rmod.is_owner_held(rec):
        say('OWNER HOLD (%s): %s' % (rmod.owner_hold_label(rec), rec.get('owner_ask')))
    say('')
    say((rec.text or '').rstrip('\n'))
    return 0


def do_list(args, ctx, snap):
    banner(snap)
    ranks = omod.ranked(snap)
    recs = snap.readable
    if args.area:
        recs = [r for r in recs if r.get('area') == args.area]
    if args.needs_priority:
        recs = sorted((r for r in recs if omod.classify(r)[0] in
                       (omod.UNPRIORITISED_OPEN, omod.OWNER_PRIORITISED)), key=lambda r: r.id)
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
    for res in checkmod.check_store(snap):
        say(res.line())
        for f in res.findings:
            say('    FAIL %s:%s %s' % (f.path, f.line, f.message))
        for f in res.warnings:
            say('    WARN %s:%s %s' % (f.path, f.line, f.message))
        red = red or res.red
    say('RED' if red else 'GREEN')
    return 1 if red else 0


def do_landable(args, ctx, snap, backend):
    worst = 0
    for v in landmod.check(backend, snap, ctx.root, args.branches):
        if v.verdict == landmod.LANDABLE:
            say('LANDABLE %s: no record is pulled by it (%s)' % (v.branch, v.detail))
        elif v.verdict == landmod.CANNOT_TELL:
            say('CANNOT TELL %s: %s' % (v.branch, v.detail))
            worst = max(worst, 1)
        else:
            worst = 2
            for rid in v.held:
                say('NOT LANDABLE %s: %s is still pulled by it; landing deletes the branch and '
                    'would leave the pull dead.' % (v.branch, rid))
                say('  Done:     python tools/backlog.py close %s --reason "<what was done>"' % rid)
                say('  Not done: python tools/backlog.py --as %s release %s --reason "<why>"'
                    % (v.branch, rid))
    return worst


# --- writes ---------------------------------------------------------------------------------------

def set_changes(args):
    return [(key, getattr(args, 'f_' + key)) for key in FIELD_FLAGS
            if getattr(args, 'f_' + key) is not None]


def planner(args, ctx):
    v = args.verb
    if v == 'add':
        return lambda s: pmod.plan_add(s, ctx, args.id, args.summary, args.scope, args.effort,
                                       args.ref, args.reopen, session=ctx.session)
    if v == 'set':
        body = pmod.read_body_file(args.body_file) if args.body_file else None
        changes = set_changes(args)
        return lambda s: pmod.plan_set(s, ctx, args.id, changes, args.unset, args.ruling, body,
                                       args.hold_ruling)
    if v == 'block':
        return lambda s: pmod.plan_block(s, ctx, args.id, args.on, args.owner_ask, args.ruling,
                                         reason=args.reason)
    if v == 'retarget':
        return lambda s: retmod.plan_retarget(s, args.old, args.new)
    if v == 'unblock':
        return lambda s: pmod.plan_unblock(s, ctx, args.id, args.ruling, args.on, args.reason)
    if v == 'pull':
        return lambda s: pmod.plan_pull(s, ctx, args.id, args.reason, args.ruling, args.blocker)
    if v == 'release':
        return lambda s: pmod.plan_release(s, ctx, args.id, args.reason)
    if v == 'close':
        return lambda s: pmod.plan_close(s, ctx, args.id, args.reason, args.ruling)
    if v == 'set-status':
        return lambda s: pmod.plan_set_status(s, ctx, args.id, args.status, args.q)
    if v == 'unpark':
        return lambda s: pmod.plan_unpark(s, ctx, args.id, args.ruling)
    return lambda s: pmod.plan_retire(s, ctx, args.id, args.why, args.ruling)


def do_write(args, ctx, store):
    change, tip = store.write(planner(args, ctx))
    for line in change.lines:
        say(line)
    if not change.writes:
        say('nothing changed; nothing was written')
        return 0
    say('WROTE %s -> %s' % (store.backend.describe(), (tip or 'the worktree')[:10]))
    say('  %s' % change.subject)
    return 0


def do_retarget_dry(args, store):
    """`retarget` without `--apply`: the change it would write, planned against a fresh read and
    checked as a write would be; nothing is published."""
    snap = store.load()
    smod.require_readable(snap)
    change = retmod.plan_retarget(snap, args.old, args.new)
    if change.writes:
        smod.check_change(snap, change)
    for line in change.lines:
        say(line)
    say('DRY RUN: nothing was written%s' % ('; `--apply` writes it as one store commit'
                                            if change.writes else ''))
    return 0


# --- start a store --------------------------------------------------------------------------------

def do_migrate(args, top, cfg, rules):
    """`<dir>/*.md` at main's tip (the remote's, fetched) -> the new store's root commit
    (kitlib.backlog.migrate; docs/backlog-store.md "Migration")."""
    configured = cfg.section('backlog')['store']
    to = args.to or (configured if configured in ('ref', 'branch') else None)
    if not to:
        raise Usage('the configured store is `%s`, not a `ref` or `branch` store; name the store '
                    'to start with `--to ref` or `--to branch`' % configured)
    rel = args.dir or ('docs/backlog' if configured in ('ref', 'branch', 'main') else configured)
    backend = smod.open_backend(top, cfg, fetch=False, store=to)
    label, src = mmod.main_tip(top, backend.remote, cfg.main_branch)
    res = mmod.migrate(backend, rules, top, src, rel, dry_run=args.dry_run, label=label,
                       session=args.session)
    say('MIGRATE %s/*.md at %s %s -> %s' % (rel.strip('/'), label, src, backend.describe()))
    for line in mmod.summary_lines(res.plan):
        say('  ' + line)
    for f in res.findings:
        say('    FAIL %s:%s %s' % (f.path, f.line, f.message))
    if res.findings:
        say('%d FAIL finding(s) copied as they are; `check` shows them, and ordinary writes fix '
            'them' % len(res.findings))
    if res.tip is None:
        say('DRY RUN: nothing was written')
    else:
        say('MIGRATED -> %s at %s' % (backend.describe(), res.tip[:10]))
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.verb == 'retire' and args.owner_said is not None:
            raise Usage('`--owner-said` is gone: owner words are never copied into a record. '
                        'Retiring an owner-held record takes `--ruling R-nnnn`')
        root = os.path.abspath(args.root or os.getcwd())
        top, why = gitrepo.toplevel(root)
        if not top:
            raise Failure(why)
        try:
            cfg = load_config(top)
        except ConfigError as exc:
            raise Failure(str(exc))
        section = cfg.section('backlog')
        rules = rmod.Rules(section['id_grammar'], section['phases'])
        if args.verb == 'migrate':
            return do_migrate(args, top, cfg, rules)
        backend = smod.open_backend(top, cfg, fetch=not args.no_fetch)
        store = smod.Store(backend, rules, session=args.session)
        ctx = pmod.Context(store, top, remote=cfg.remote, main_branch=cfg.main_branch,
                           branch=args.branch)
        if args.verb in WRITES or (args.verb == 'retarget' and args.apply):
            return do_write(args, ctx, store)
        if args.verb == 'retarget':
            return do_retarget_dry(args, store)
        snap = store.load()
        if args.verb == 'landable':
            return do_landable(args, ctx, snap, backend)
        return {'next': do_next, 'show': do_show, 'list': do_list,
                'check': do_check, 'stale': do_stale}[args.verb](args, ctx, snap)
    except omod.PartitionError as exc:
        say('ERROR: the partition does not add up: %s' % exc)
        return 1
    except Refusal as exc:
        say('%s: %s' % (exc.label, exc))
        return exc.code


if __name__ == '__main__':
    sys.exit(main())
