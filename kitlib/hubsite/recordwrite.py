"""kitlib.hubsite.recordwrite: the Action Hub's writes to a repository's backlog store, the owner's
add, edit and delete from the record page (docs/action-hub.md "The record page").

  add(root, rid, summary, scopes, effort=None, ref=None)        -> {"lines"}
  edit(root, rid, version, changes, unset=(), body=None, ruling=None) -> {"lines"}
  delete(root, rid, why, ruling=None)                           -> {"lines"}

Each opens the store `.kit.json` in `root` names (kitlib.backlog.opener open_store, fetched first)
and writes through Store.write with the plan functions backlog.py runs (kitlib.backlog.plan
plan_add, plan_set, plan_delete), so every store check applies. "lines" is what the store said
about the write, its notes included.

add writes the review mark `owner-hub <date>`, found on no branch: pull and next refuse the record
until a coordinator clears it. edit is pinned to the record text the page read: `version` is
Views.record's `<commit>:<sha1 of the text>`, and a record whose text no longer has that sha1 is a
`conflict`, checked again on every re-plan. `ruling` is the PM ruling id the store asks for on an
owner-held or owner-priced record; the page sends it only when a refusal asked.

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
        return pmod.plan_set(s, ctx, rid, list(changes), list(unset), ruling, body)
    return _write(root, plan)


def delete(root, rid, why, ruling=None):
    return _write(root, lambda s, ctx: pmod.plan_delete(s, ctx, rid, why, ruling))
