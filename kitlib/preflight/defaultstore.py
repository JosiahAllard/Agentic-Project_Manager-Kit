"""preflight's `backlog` line: the implicit default store refs/backlog/main still holding records
while it is not the live store (docs/preflight.md).

The ref is the live store only when `.kit.json` names `"backlog": {"store": "ref"}`. Named another
store, the ref is a stale copy. Named none, it is read-only (kitlib/backlog/store.py
`default_store_refusal`) and `backlog.py -C <this checkout>` reads it, so a worker sees stale
records (a pull NOT FOUND, a wrong `next`). Either way one WARN line names the ref, its record
count and the command that reads the live store.

Cheap, because preflight runs at every agent start: nothing when the ref is named; else
refs.ref_commit (one `for-each-ref`, and a second of refs/ to confirm "no ref" when it is absent),
one `ls-tree` when the ref exists (store.record_entries, no
blob read) and, only on the warn path with no store named, the PM data lookup
(at_landing.pm_data_root). The local ref is read as it stands, never fetched. Never red, never
raises: a failure is a `not checked` WARN line, and the exit code is preflight's own.
"""
import os

from kitlib.backlog import store as smod
from kitlib.git import refs
from kitlib.preflight import nextblock

LABEL = 'backlog   '


def lines(top, cfg, tools):
    """`[]` or the one line for the checkout whose top level is `top`; `tools` is the kit's tools
    folder, whose backlog.py the line names."""
    try:
        return _lines(top, cfg, tools)
    except Exception as e:               # noqa: BLE001 - a WARN line must never become the failure
        return [LABEL + 'WARN default store not checked: %s: %s' % (type(e).__name__, e)]


def _lines(top, cfg, tools):
    named = cfg.written('backlog', 'store')
    if named == 'ref':
        return []
    tip, why = refs.ref_commit(smod.STORE_REF, top)
    if tip is None:
        return [LABEL + 'WARN default store not checked: %s' % why]
    if not tip:
        return []
    entries, why = smod.record_entries(top, tip, smod.STORE_DIR)
    if entries is None:
        return [LABEL + 'WARN default store not checked: %s' % why]
    if not entries:
        return []
    head = (LABEL + 'WARN the default store %s holds %d record(s) here (local ref, not fetched)'
            % (smod.STORE_REF, len(entries)))
    command = nextblock.command(tools, 'backlog')
    if named is not None:
        return [head + ', but .kit.json names "backlog".store "%s"; read the configured store: '
                '%s -C "%s" list' % (named, command, _slash(top))]
    head += ' and .kit.json names no "backlog".store'
    from kitlib.backlog import at_landing        # only on the warn path: it loads the config code
    pm_root, why = at_landing.pm_data_root(top)
    if pm_root is not None and _same(pm_root, top):
        pm_root, why = None, None
    if pm_root is not None:
        return [head + ', so backlog.py -C on this checkout reads that copy; the live store: '
                '%s -C "%s" list' % (command, _slash(pm_root))]
    if why:
        return [head + ", so backlog.py -C on this checkout reads that copy; the live store's PM "
                'data worktree was not found (%s)' % why]
    return [head + ', so it is read-only; if it is the live store, name it in .kit.json: '
            '"backlog": {"store": "ref"}']


def _slash(path):
    return path.replace('\\', '/')


def _same(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))
