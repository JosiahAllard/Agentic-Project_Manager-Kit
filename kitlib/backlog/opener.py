"""Open the configured backlog store from a worktree, and map a write verb to its plan (moved
from tools/backlog.py `main`, `planner` and `do_retarget_dry`, behaviour unchanged;
docs/backlog.md).

  open_repo(root)   -> (top, cfg, rules); Failure when `root` is no repository or its `.kit.json`
                       cannot be read
  open_store(root, fetch=True, session=None, branch=None) -> plan.Context over the store
  planner(args, ctx) -> the plan `Store.write` runs for `args.verb` (one of WRITES, or `retarget`);
                       Usage for any other verb, raised before a plan exists
  retarget_dry(args, store) -> the Change `retarget` would write, checked as a write would be;
                       nothing is published

`planner` and `retarget_dry` take an object with tools/backlog.py's argument names (`verb`, `id`,
`f_<field>`, `old`, `new`...).
"""
from kitlib.config import ConfigError, load as load_config
from kitlib.git import repo as gitrepo

from . import plan as pmod
from . import pull_branch as pullmod
from . import record as rmod
from . import retarget as retmod
from . import store as smod
from .errors import Failure, Usage

# `set --<field>` for every field but hold_ruling, whose `--hold-ruling` is the recite.
FIELD_FLAGS = [k for k in rmod.FIELDS if k != 'hold_ruling']
WRITES = ('add', 'set', 'block', 'unblock', 'pull', 'release', 'close', 'retire', 'delete',
          'set-status', 'unpark', 'clear-review')


def open_repo(root):
    top, why = gitrepo.toplevel(root)
    if not top:
        raise Failure(why)
    try:
        cfg = load_config(top)
    except ConfigError as exc:
        raise Failure(str(exc))
    section = cfg.section('backlog')
    rules = rmod.Rules(section['id_grammar'], section['phases'])
    return top, cfg, rules


def open_store(root, fetch=True, session=None, branch=None):
    top, cfg, rules = open_repo(root)
    section = cfg.section('backlog')
    backend = smod.open_backend(top, cfg, fetch=fetch)
    store = smod.Store(backend, rules, session=session)
    ctx = pmod.Context(store, top, remote=cfg.remote, main_branch=cfg.main_branch,
                       branch=branch, code_main=section['code_main'] or cfg.main_branch)
    return ctx


def set_changes(args):
    return [(key, getattr(args, 'f_' + key)) for key in FIELD_FLAGS
            if getattr(args, 'f_' + key) is not None]


def planner(args, ctx):
    v = args.verb
    if v == 'add':
        return lambda s: pmod.plan_add(s, ctx, args.id, args.summary, args.scope, args.effort,
                                       args.ref, args.reopen, session=ctx.session,
                                       found_on=args.found_on, why_not_folded=args.why_not_folded)
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
        return lambda s: pullmod.plan_pull(s, ctx, args.id, args.reason, args.ruling,
                                           args.blocker)
    if v == 'release':
        return lambda s: pmod.plan_release(s, ctx, args.id, args.reason)
    if v == 'close':
        return lambda s: pmod.plan_close(s, ctx, args.id, args.reason, args.ruling)
    if v == 'set-status':
        return lambda s: pmod.plan_set_status(s, ctx, args.id, args.status, args.q)
    if v == 'unpark':
        return lambda s: pmod.plan_unpark(s, ctx, args.id, args.ruling)
    if v == 'retire':
        return lambda s: pmod.plan_retire(s, ctx, args.id, args.why, args.ruling)
    if v == 'delete':
        return lambda s: pmod.plan_delete(s, ctx, args.id, args.why, args.ruling)
    if v == 'clear-review':
        return lambda s: pmod.plan_clear_review(s, ctx, args.id, args.reason)
    # The caller's mistake, so Usage (errors.py); raised here, before any plan could run.
    raise Usage('`%s` is not a write verb: `planner` plans %s' % (v, ', '.join(WRITES + ('retarget',))))


def retarget_dry(args, store):
    """`retarget` without `--apply`: the change it would write, planned against a fresh read and
    checked as a write would be; nothing is published."""
    snap = store.load()
    smod.require_readable(snap)
    with smod.plan_notes() as notes, smod.notes_on_refusal(notes):
        change = retmod.plan_retarget(snap, args.old, args.new)
    change = smod.with_plan_notes(change, notes)
    if change.writes:
        with smod.notes_on_refusal(notes):
            smod.check_change(snap, change)
    return change
