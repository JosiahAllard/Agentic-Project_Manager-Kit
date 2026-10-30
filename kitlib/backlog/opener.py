"""Open the configured backlog store from a worktree, and map a write verb to its plan (moved
from tools/backlog.py `main`, `planner` and `do_retarget_dry`, behaviour unchanged;
docs/backlog.md).

  open_repo(root)   -> (top, cfg, rules); Failure when `root` is no repository or its `.kit.json`
                       cannot be read
  open_store(root, fetch=True, session=None, branch=None) -> plan.Context over the store, its
                       `liveness` probe the worktree classifier's (liveness), its `cap` repo_cap's
  repo_cap(top)     -> the main checkout's `.kit.json` backlog.cap `next` and `pull` share
  planner(args, ctx) -> the plan `Store.write` runs for `args.verb` (one of WRITES, `retarget` or
                       `uncite`); Usage for any other verb, raised before a plan exists
  retarget_dry(args, ctx) -> the Change `retarget` would write, checked as a write would be;
                       nothing is published
  uncite_dry(ctx)    -> the same for `uncite` (plan.plan_uncite)

`planner` and `retarget_dry` take an object with tools/backlog.py's argument names (`verb`, `id`,
`f_<field>`, `old`, `new`...).
"""
import kitlib.config as kconfig
from kitlib.config import ConfigError, load as load_config
from kitlib.git import repo as gitrepo

from . import plan as pmod
from . import policy
from . import pull_branch as pullmod
from . import record as rmod
from . import retarget as retmod
from . import store as smod
from .errors import Failure, Refusal, Usage

# `set --<field>` for every field; plan_set refuses the ones a verb writes (plan.WRITTEN_BY).
FIELD_FLAGS = list(rmod.FIELDS)
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
    code_main, code_remote = section['code_main'], cfg.remote
    code = pullmod.code_config(top)
    if code is not None:
        # The PM data worktree: its own main and remote are the data's; the code lands on the
        # main checkout's (docs/backlog.md "The fold-in guard").
        code_main, code_remote = code_main or code.main_branch, code.remote
    ctx = pmod.Context(store, top, remote=cfg.remote, main_branch=cfg.main_branch,
                       branch=branch, code_main=code_main or cfg.main_branch,
                       code_remote=code_remote, liveness=lambda name: liveness(top, name),
                       cap=lambda: repo_cap(top))
    return ctx


def repo_cap(top):
    """The cap `next` offers up to and `pull` checks against (plan.Context.cap, read lazily, so
    only an offer reads it): `.kit.json` "backlog".cap of the repository's main checkout, its
    first worktree (as pull_branch._main_checkout finds it), whatever checkout `top` is. A worker
    worktree cut before the owner set the cap, or the PM data worktree's data branch, would
    otherwise read a value the main checkout's `next` does not. A bare first worktree: `top`'s
    own. Main checkout config that cannot be read, or worktrees git cannot list, is a Failure
    (ERROR), never a silent default; the schema refuses a value that is not a whole number >= 1
    when the file loads (kitlib/config/schema.py), and checked_cap refuses one that reaches here
    otherwise."""
    wts, why = gitrepo.worktrees(top)
    if not wts:
        raise Failure('cannot read the backlog cap: git cannot list the worktrees of %s, so its '
                      'main checkout\'s .kit.json is unknown (%s)' % (top, why))
    main = top if wts[0].bare else wts[0].path
    try:
        cfg = kconfig.load(main)
    except (kconfig.ConfigError, OSError) as exc:
        raise Failure('cannot read the backlog cap from the main checkout %s: %s' % (main, exc))
    return checked_cap(cfg.section('backlog')['cap'], main)


def checked_cap(cap, where):
    """`cap` when it is a whole number >= 1, else a Refusal naming `where`."""
    if type(cap) is not int or cap < 1:
        raise Refusal('.kit.json "backlog".cap of %s is %r: it must be a whole number >= 1'
                      % (where, cap))
    return cap


def liveness(top, branch):
    """plan.Context's `liveness` probe: kitlib.worktree.classify.branch_liveness on the repository
    `top` is in. Imported here, when a block or release asks, never at load: kitlib.worktree
    imports this package's store."""
    from kitlib.worktree import classify
    return classify.branch_liveness(top, branch)


def set_changes(args):
    return [(key, getattr(args, 'f_' + key)) for key in FIELD_FLAGS
            if getattr(args, 'f_' + key) is not None]


def planner(args, ctx):
    v = args.verb
    if v == 'add':
        body = pmod.read_body_file(args.body_file) if args.body_file else None
        return lambda s: pmod.plan_add(s, ctx, args.id, args.summary, args.scope, args.effort,
                                       args.ref, args.reopen, session=ctx.session,
                                       found_on=args.found_on, why_not_folded=args.why_not_folded,
                                       body=body, summary_max=pmod.SUMMARY_MAX)
    if v == 'set':
        body = pmod.read_body_file(args.body_file) if args.body_file else None
        changes = set_changes(args)
        # A widening into a live pull is the plan's to find; the role decides whether it may.
        may_collide = policy.may(pmod.WIDEN_ROW)
        return lambda s: pmod.plan_set(s, ctx, args.id, changes, args.unset, body=body,
                                       role_checked=True, may_collide=may_collide)
    if v == 'block':
        return lambda s: pmod.plan_block(s, ctx, args.id, args.on, args.owner_ask,
                                         reason=args.reason, until=args.until)
    if v == 'retarget':
        return lambda s: retmod.plan_retarget(s, ctx, args.old, args.new)
    if v == 'uncite':
        return lambda s: pmod.plan_uncite(s, ctx)
    if v == 'unblock':
        return lambda s: pmod.plan_unblock(s, ctx, args.id, args.on, args.reason,
                                           role_checked=True)
    if v == 'pull':
        may_collide = policy.may('pull-collision')
        return lambda s: pullmod.plan_pull(s, ctx, args.id, args.reason, may_collide,
                                           args.blocker)
    if v == 'release':
        return lambda s: pmod.plan_release(s, ctx, args.id, args.reason)
    if v == 'close':
        return lambda s: pmod.plan_close(s, ctx, args.id, args.reason, role_checked=True)
    if v == 'set-status':
        return lambda s: pmod.plan_set_status(s, ctx, args.id, args.status, args.q)
    if v == 'unpark':
        return lambda s: pmod.plan_unpark(s, ctx, args.id)
    if v == 'retire':
        return lambda s: pmod.plan_retire(s, ctx, args.id, args.why, role_checked=True)
    if v == 'delete':
        return lambda s: pmod.plan_delete(s, ctx, args.id, args.why, role_checked=True)
    if v == 'clear-review':
        return lambda s: pmod.plan_clear_review(s, ctx, args.id, args.reason)
    # The caller's mistake, so Usage (errors.py); raised here, before any plan could run.
    raise Usage('`%s` is not a write verb: `planner` plans %s'
                % (v, ', '.join(WRITES + ('retarget', 'uncite'))))


def retarget_dry(args, ctx):
    """`retarget` without `--apply`: the change it would write, planned against a fresh read and
    checked as a write would be; nothing is published."""
    return _dry(ctx, lambda snap: retmod.plan_retarget(snap, ctx, args.old, args.new))


def uncite_dry(ctx):
    """`uncite` without `--apply`: the change it would write (its NEEDS HAND EDIT list included),
    planned and checked as `retarget_dry`'s is; nothing is published."""
    return _dry(ctx, lambda snap: pmod.plan_uncite(snap, ctx))


def _dry(ctx, plan):
    snap = ctx.store.load()
    smod.require_readable(snap)
    with smod.plan_notes() as notes, smod.notes_on_refusal(notes):
        change = plan(snap)
    change = smod.with_plan_notes(change, notes)
    if change.writes:
        with smod.notes_on_refusal(notes):
            smod.check_change(snap, change)
    return change
