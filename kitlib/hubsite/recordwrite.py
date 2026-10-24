"""kitlib.hubsite.recordwrite: the Action Hub's writes to a repository's backlog store, the owner's
add, edit, delete and unblock from the record page (docs/action-hub.md "The record page").

  add(root, rid, summary, scopes, effort=None, ref=None)        -> {"lines"}
  edit(root, rid, version, changes, unset=(), body=None, ruling=None) -> {"lines"}
  delete(root, rid, why, ruling=None)                           -> {"lines"}
  unblock(root, rid, on=None, reason=None, ruling=None)         -> {"lines"}
  answer(root, rid, qid, at, title, label, note=None, via=None, other=False, waiting=())
                                                                -> {"lines"}

Each opens the store `.kit.json` in `root` names (kitlib.backlog.opener open_store, fetched first)
and writes through Store.write with the plan functions backlog.py runs (kitlib.backlog.plan
plan_add, plan_set, plan_delete, plan_unblock, plan_answer), so every store check applies.
"lines" is what the store said about the write, its notes included.

answer files the owner's answer to hub question `qid` (answered at `at`) into the record the
question is bound to (docs/action-hub.md "A question bound to a record"): the hub calls it after
the answer is stored, and again on a retry, so the same `qid` and `at` changes nothing and an
older `at` never overwrites a newer answer (plan_answer). `waiting` is the hub's other questions
on the record still open. It runs with the role check made (role_checked): the hub files only the
answer the owner gave, whoever's call starts the filing.

add writes the review mark `owner-hub <date>`, found on no branch: pull and next refuse the record
until a coordinator clears it. edit is pinned to the record text the page read: `version` is
Views.record's `<commit>:<sha1 of the text>`, and a record whose text no longer has that sha1 is a
`conflict`, checked again on every re-plan. `ruling` is accepted and ignored: these are owner
routes, so the plans run with the role check already made (role_checked) and the store asks for
no ruling; the page's ruling field goes with the next slice of that removal. unblock clears
one blocker as the backlog tool's unblock does: a record blocked on several needs `on` (the one
to clear) and a `reason`, and with no `on` it is refused here as `invalid` on `on`, since the
store's own refusal names the backlog tool's path.

What is raised is a HubError: NotFound `not_found`; Usage and any other refusal `invalid`, with the
store's own text; a Failure (git, a publish, a folder store outside a linked worktree) `store`,
whose text the Api gives the local owner only (kitlib.hubsite.api "What an agent is told"). The
implicit `ref` store (no "backlog".store in `.kit.json`) is refused as `store` before any write,
since its refusal names the PM data worktree's path. Anything else is raised as it is.
"""
import hashlib

from kitlib.backlog import opener
from kitlib.backlog import plan as pmod
from kitlib.backlog import scope as bscope
from kitlib.backlog import store as bstore
from kitlib.backlog.errors import Failure, NotFound, Refusal

from .store import HubError

SESSION = 'owner-hub'               # the commit's Session: line and the review mark's source
NOT_FOLDED = 'added by the owner from the Action Hub'
STALE = 'the record changed since this page read it: read it again'


def _open(root):
    try:
        ctx = opener.open_store(root, fetch=True, session=SESSION)
    except Failure as e:
        raise HubError('store', str(e), 'repo')
    if getattr(ctx.store.backend, 'implicit', False):
        raise HubError('store', bstore.default_store_refusal(ctx.root), 'repo')
    return ctx


def _write(root, plan):
    """Store.write(plan(snapshot, ctx)) on the store at root, its errors as HubErrors."""
    ctx = _open(root)
    try:
        change, _tip = ctx.store.write(lambda s: plan(s, ctx))
    except NotFound as e:
        raise HubError('not_found', str(e), 'id')
    except Failure as e:
        raise HubError('store', str(e), '')
    except Refusal as e:
        raise HubError('invalid', str(e), '')
    return {'lines': list(change.lines)}


def add(root, rid, summary, scopes, effort=None, ref=None):
    def plan(s, ctx):
        return pmod.plan_add(s, ctx, rid, summary, list(scopes), effort, ref, session=None,
                             found_on=bscope.NONE, why_not_folded=NOT_FOLDED,
                             review='%s %s' % (SESSION, ctx.date()))
    return _write(root, plan)


def edit(root, rid, version, changes, unset=(), body=None, ruling=None):
    want = str(version).rsplit(':', 1)[-1]

    def plan(s, ctx):
        rec = s.get(rid)
        if rec is not None:
            now = s.raw.get(rid)
            if now is None:
                now = rec.text.encode('utf-8')
            if hashlib.sha1(now).hexdigest() != want:
                raise HubError('conflict', STALE, 'version')
        # an OWNER route (kitlib/hubsite/api.py): `ruling` is ignored, see the module docstring
        return pmod.plan_set(s, ctx, rid, list(changes), list(unset), None, body,
                             role_checked=True, may_collide=True)
    return _write(root, plan)


def delete(root, rid, why, ruling=None):
    return _write(root, lambda s, ctx: pmod.plan_delete(s, ctx, rid, why,
                                                        role_checked=True))   # an OWNER route


def unblock(root, rid, on=None, reason=None, ruling=None):
    def plan(s, ctx):
        rec = s.get(rid)
        if rec is not None and on is None and rec.get('state') == 'blocked':
            members = rec.all('blocked_on')
            if len(members) > 1:
                raise HubError('invalid', '%s is blocked on %s: name the one to clear (on) and '
                               'give a reason' % (rid, ', '.join(members)), 'on')
        return pmod.plan_unblock(s, ctx, rid, on, reason, role_checked=True)   # an OWNER route
    return _write(root, plan)


def answer(root, rid, qid, at, title, label, note=None, via=None, other=False, waiting=()):
    # the owner's stored answer (module docstring), so the unblock-owner row's check is made
    return _write(root, lambda s, ctx: pmod.plan_answer(
        s, ctx, rid, qid, at, title, label, note or None, via or None, other, list(waiting),
        role_checked=True))
