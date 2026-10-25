"""kitlib.dailyfull.file: the backlog record a daily bisect files for the red it reproduced
(docs/daily-full.md "Bisect").

The record's id is `<prefix>-DAILY-RED-<SHA10>`, where SHA10 is the culprit's commit, or the red
run's commit when the bisect named only a range. Its scope is the paths the culprit changed
against its first parent (`none` when there is no culprit, none of its paths is a valid scope
member, or more than MAX_SCOPE are). Its ref is `daily-bisect <sha>`.

The write goes through the backlog library (kitlib.backlog.opener open_store, fetched first, and
plan_add / plan_set in Store.write), so every store check applies. When the repository's store
is the implicit default one, the PM data branch's store is used instead
(kitlib.backlog.at_landing.pm_data_store).

The record is priced in the same call. The machine config's `daily_red_ruling`
(kitlib.config.user), read at filing time, is only an on/off switch: its value is never written
into the record or its commit.

- set (non-empty): priority 1, priority_by pm, written as a role-checked set (the owner turned
  it on in the machine config, the role check's stand-in for this run);
- unset: priority 2, priority_by agent (an agent's own price never takes priority 1), and the
  returned note says so for the run's result.json.

Filing is idempotent. An id the store already holds is not added again, and a record already
priced is never repriced. Nothing is filed without a prefix. Nothing here prints or exits: file()
returns the lines to say.
"""
from kitlib.backlog import at_landing, opener
from kitlib.backlog import plan as pmod
from kitlib.backlog import scope as bscope
from kitlib.backlog import store as bstore
from kitlib.backlog.errors import Refusal
from kitlib.config import user as kuser
from kitlib.config.types import ConfigError

SLUG = 'daily-full'                  # the store commit's Session: line
MAX_SCOPE = 20
NOT_FOLDED = 'found by the daily full run bisect on the main branch, with no branch to fold it into'
PRICE_PM, PRICE_AGENT = '1', '2'
NO_RULING = ('no daily_red_ruling is set in the machine config: a new record is priced at '
             'priority 2, priority_by agent')


def record_id(prefix, sha):
    return '%s-DAILY-RED-%s' % (prefix, sha[:10].upper())


def culprit_scope(root, culprit, git):
    """The paths `culprit` changed against its first parent, as scope members; ['none'] when
    there are none, any is not a valid member, or more than MAX_SCOPE."""
    if not culprit:
        return [bscope.NONE]
    rc, out, _ = git(root, 'diff', '--name-only', '%s^1' % culprit, culprit)
    paths = out.split('\n') if rc == 0 and out else []
    paths = [p.strip() for p in paths if p.strip()]
    if not paths or len(paths) > MAX_SCOPE or any(bscope.scope_member_error(p) for p in paths):
        return [bscope.NONE]
    return paths


def open_filing_store(root, session=SLUG):
    """`(Context, None)` for the store a kit tool files a found record in: the repository's
    configured one, or the PM data branch's when the configured one is the implicit default;
    fetched, its commits carrying `session` as their Session: line. `(None, why)` when there is
    neither. Raises Refusal (the store's own). kitlib.maintlint.gatetime files through it too."""
    ctx = opener.open_store(root, fetch=True, session=session)
    if not getattr(ctx.store.backend, 'implicit', False):
        return ctx, None
    pm_root, _cfg, _backend, why = at_landing.pm_data_store(root, None)
    if pm_root is None:
        return None, why or bstore.default_store_refusal(ctx.root)
    return opener.open_store(pm_root, fetch=True, session=session), None


def file(root, settings, found, red, git, home=None):
    """Files the bisect `found` (kitlib.dailyfull.bisect.bisect, with `bad`) of the red steps
    `red`, through the store of the repository at `root`. `settings`: the "daily_full".bisect
    section; `home`: the machine config's home (None: the user's). Returns
    `(rid or None, lines, note)`: `note` is NO_RULING when the switch is off (the record is
    priced at priority 2 by an agent), else None. Every store refusal is a line, never raised."""
    prefix = (settings or {}).get('prefix')
    if not prefix:
        return None, [], None
    sha = found.get('culprit') or found.get('bad')
    if not sha:
        return None, [], None
    rid = record_id(prefix, sha)
    try:
        # An on/off switch only: the value is never passed on or written.
        pm_price = bool(kuser.daily_red_ruling(home))
    except ConfigError as e:
        return None, ['daily: not filed %s: the machine config: %s' % (rid, e)], None
    note = None if pm_price else NO_RULING
    price = [('priority', [PRICE_PM]), ('priority_by', ['pm'])] if pm_price else \
        [('priority', [PRICE_AGENT]), ('priority_by', ['agent'])]
    if found.get('culprit'):
        summary = ('the daily full run turned red (%s) at commit %s, found by bisect in %d probe(s)'
                   % (', '.join(red), sha[:10], len(found.get('probes') or ())))
    else:
        summary = ('the daily full run is red (%s) at %s; its bisect named no single commit: %s'
                   % (', '.join(red), sha[:10], found.get('verdict')))
    scopes = culprit_scope(root, found.get('culprit'), git)
    lines = []
    try:
        ctx, why = open_filing_store(root)
        if ctx is None:
            return None, ['daily: not filed: %s' % why], None

        def add(s):
            if s.get(rid) is not None:
                return bstore.Change({}, '', ['%s is already filed' % rid])
            return pmod.plan_add(s, ctx, rid, summary, scopes, None, 'daily-bisect %s' % sha,
                                 session=settings.get('session'), found_on=bscope.NONE,
                                 why_not_folded=NOT_FOLDED)

        def priced(s):
            rec = s.get(rid)
            if rec is None or rec.get('priority') != '-':
                return bstore.Change({}, '', [])
            return pmod.plan_set(s, ctx, rid, price, (), role_checked=pm_price)

        for plan in (add, priced):
            change, _tip = ctx.store.write(plan)
            lines.extend('daily: %s' % line for line in change.lines)
    except (Refusal, ConfigError, OSError) as e:
        return None, lines + ['daily: not filed %s: %s' % (rid, e)], None
    return rid, lines + (['daily: %s' % note] if note else []), note
