"""The rulings ledger as the backlog reads it.

The ledger is read only when the machine config names a `ledger_path` and that file exists
(a repository cannot supply its own); with none, a ruling is checked for its shape only and
`check` skips the ruling checks. The standing delegation, an entry that delegates in general and
so authorises no one change, is the machine config's `standing_delegation` (none when unset).
An entry is `## R-nnnn | <date> | <repo> | <kind>`; only its header and its last non-blank line
are read (kitlib.ledger.read, the one reader). A last line `Items: A, B` names the records the
ruling may release; no such line, or a malformed one, names nobody. A tombstone (`Retired: <class> - <why> (see R-n|-)` as the whole
body) is refused naming its live successor.
"""
import os

from kitlib.ledger import read

from . import record as rmod
from .errors import Failure, Refusal, Usage

OWNER_KINDS = ('typed', 'selected', 'relayed-from-terminal')
DELEGATED_KIND = 'delegated'
HOLD_KINDS = OWNER_KINDS + (DELEGATED_KIND,)

# The entry type and the reader are kitlib.ledger.read's: one reader for every consumer.
Entry = read.Entry
read_entries = read.read_entries


NAME_KEYS = ('backlog.ledger_repo', 'repo.name')


def ledger_name(cfg):
    """The ledger repo column that rules in the repository `cfg` was read from, lower-cased:
    `backlog.ledger_repo`, else `repo.name`, else ''. Never the folder name: a folder named
    `project-manager` whose ledger column is `pm` would silently rule nothing."""
    return (cfg.section('backlog')['ledger_repo'] or cfg.repo.get('name') or '').lower()


class Ledger(object):
    """`path` None (or a missing file): not configured. `repos` the repo columns that rule here;
    none is a config error (`name_error`), never a column that matches nobody. `where` names the
    file the name is set in. `standing` the standing delegation's id, refused as the ruling behind
    any one change (None: none)."""

    def __init__(self, path, repos, standing=None, where=None):
        self.path = path
        self.standing = standing or None
        self.repos = tuple(r.lower() for r in repos if r)
        self.where = where
        self._entries = None
        self._why = None

    @classmethod
    def for_repo(cls, path, cfg, standing=None):
        """The ledger as the repository `cfg` was read from sees it (`ledger_name`)."""
        return cls(path, [ledger_name(cfg)], standing, where=cfg.where)

    @property
    def configured(self):
        return bool(self.path) and os.path.isfile(self.path)

    @property
    def name_error(self):
        """None when a repo column rules here; else the config error that says what to set."""
        if self.repos:
            return None
        return ('no rulings-ledger repo name: set %s in %s. The ledger\'s repo column says '
                'which repository a ruling binds, so no ruling can be checked until one is set'
                % (' or '.join('`%s`' % k for k in NAME_KEYS), self.where or '.kit.json'))

    def entries(self):
        """`({id: Entry}, None)` or `(None, why)`."""
        if self._entries is None and self._why is None:
            self._entries, self._why = read_entries(self.path)
        return self._entries, self._why


def successor(entries, rid):
    """The live id a tombstone's `see` chain ends at, or None."""
    seen = set()
    while rid in entries and entries[rid].retired:
        if rid in seen:
            return None
        seen.add(rid)
        rid = entries[rid].retired[1]
        if rid == '-':
            return None
    return rid


def ruling_refusal(ledger, entries, ruling, kinds, allow_retired=False):
    """None when `ruling` is an entry this repository may cite for a write `kinds` allows."""
    got = entries.get(ruling)
    if got is not None and got.retired and not allow_retired:
        target = successor(entries, ruling)
        return ('`%s` is retired in the rulings ledger (%s): %s'
                % (ruling, got.retired[0], 'cite %s instead' % target if target else 'drop the citation'))
    if ledger.standing and ruling == ledger.standing:
        return ('`%s` is the standing delegation, not the ruling behind any one change: cite the '
                'delegated entry that names this record, or the owner ruling' % ruling)
    if ledger.name_error is not None:
        return ledger.name_error
    if got is None or got.repo not in ledger.repos:
        return ('`%s` is not a ruling header for %s in the rulings ledger%s'
                % (ruling, ' or '.join('`%s`' % r for r in ledger.repos),
                   '' if got is None else ' (its repo column is `%s`)' % got.repo))
    if got.kind not in HOLD_KINDS:
        return ('`%s` has the kind `%s`, none of %s or `%s` - an unknown kind is refused, never read '
                'as the owner\'s' % (ruling, got.kind, ', '.join(OWNER_KINDS), DELEGATED_KIND))
    if got.kind not in kinds:
        return ('`%s` is a `%s` entry, which releases an owner hold and does nothing else: it never '
                'prices and never retires. Cite the owner\'s own ruling' % (ruling, got.kind))
    return None


def items_refusal(entry, ruling, rid):
    if entry.malformed is not None:
        return ('`--ruling %s` does not name `%s`: its last line `%s` holds a token that is not a '
                'record id, so it names nobody' % (ruling, rid, entry.malformed))
    if entry.items is None:
        return ('`--ruling %s` does not name `%s`: its ledger entry has no `Items:` last line. A '
                'ruling authorises a write only for the records its entry names' % (ruling, rid))
    if rid not in entry.items:
        return ('`--ruling %s` does not name `%s`: its `Items:` line lists `%s`'
                % (ruling, rid, ', '.join(entry.items)))
    return None


def spent_hold_rulings(rec):
    """The record's own `hold_ruling` while it is blocked on the owner: a hold set again after
    that ruling released one, so the id never releases a hold here again."""
    own = rec.get('hold_ruling')
    return (own,) if own and 'owner' in rec.all('blocked_on') else ()


def hold_live(rec):
    """A `hold_ruling` that is not spent: `close` accepts it as the ruling it needs."""
    return rec.has('hold_ruling') and rec.get('hold_ruling') not in spent_hold_rulings(rec)


def refuse_unnamed_release(rid, rec, ruling, verb):
    """A `priority: -1` beside a live `hold_ruling` R was released by R: its release takes R."""
    own = rec.get('hold_ruling')
    if rec.get('priority') == rmod.OWNER_PRIORITY and hold_live(rec) and ruling != own:
        raise Refusal('`%s`\'s `priority: -1` was released by `hold_ruling: %s`, still live, so this '
                      '%s needs `--ruling %s`, not `%s`' % (rid, own, verb, own, ruling))


def check_ruling(ledger, rid, rec, ruling, verb='set', hold_only=False, needs_items=True,
                 pm_price=False):
    """`--ruling`'s refusals where an owner gate needs one: the shape, the standing delegation,
    one ruling one change, a spent id, then (configured ledger only) the entry itself."""
    if not rmod.RULING_RE.match(ruling or ''):
        raise Usage('`--ruling %s` is not a ruling id - `R-nnnn`' % ruling)
    if ledger.standing and ruling == ledger.standing:
        raise Refusal('`%s` is the standing delegation, not the ruling behind any one change'
                      % ruling)
    if rec.get('priority_ruling') == ruling:
        raise Refusal('`%s` already carries `priority_ruling: %s`. One ruling, one change: cite the '
                      'ruling that allows THIS %s' % (rid, ruling, verb))
    if ruling in spent_hold_rulings(rec):
        raise Refusal('`--ruling %s` is spent: it released an earlier hold on `%s`, which was held '
                      'again. This %s needs the ruling that releases THIS hold' % (ruling, rid, verb))
    if rec.get('hold_ruling') == ruling and not hold_only:
        raise Refusal('`%s` already carries `hold_ruling: %s`; a hold release never moves a price. '
                      'Cite the ruling that allows THIS %s' % (rid, ruling, verb))
    displaces_owner = pm_price and rec.get('priority_by') == 'owner'
    if pm_price and not needs_items and not displaces_owner:
        return
    if not ledger.configured:
        return
    entries, why = ledger.entries()
    if entries is None:
        raise Failure('`--ruling %s` cannot be checked: %s. Nothing was written' % (ruling, why))
    bad = ruling_refusal(ledger, entries, ruling, HOLD_KINDS if hold_only else OWNER_KINDS)
    if bad is None and (needs_items or displaces_owner):
        bad = items_refusal(entries[ruling], ruling, rid)
    if bad is not None:
        raise Refusal(bad)


def record_view(root, cfg, fetch=False):
    """`(ledger names, open ids, closed)` for the repository at `root`: the repo column that rules
    here (`ledger_repo`, else the repo name; `all` is the caller's), the ids of the records open in
    the store now, and `closed(id)`, true when the store's history shows a commit that closed or
    retired it. A store that cannot be read raises Failure."""
    from . import store as smod
    section = cfg.section('backlog')
    rules = rmod.Rules(section['id_grammar'], section['phases'])
    store = smod.Store(smod.open_backend(root, cfg, fetch=fetch), rules)
    snap = store.load()
    if not snap.records and snap.tip is None:
        raise Failure('%s holds no backlog store (%s)' % (root, store.backend.describe()))
    name = ledger_name(cfg)
    return ((name,) if name else ()), set(snap.by_id), lambda rid: store.history(rid, snap) is not None


def orphan_report(ledger_file, root):
    """The lines `ledger.py check` prints for the ruling-orphan scan (kitlib.ledger.orphans) of the
    repository holding `root`: WARN lines, then one summary line. The scan is skipped, with the
    reason, when `root` is no repository, its `.kit.json` or store cannot be read, or the ledger
    holds no entry. It never fails."""
    from kitlib.config import ConfigError, load as load_config
    from kitlib.git import repo as gitrepo
    from kitlib.ledger import orphans

    def skipped(why):
        return ['ruling orphans   SKIPPED - %s' % ' '.join(str(why).split())]

    top, why = gitrepo.toplevel(root)
    if not top:
        return skipped('%s is not in a repository (%s)' % (root, why))
    entries, why = read.read_entries(ledger_file)
    if entries is None:
        return skipped(why)
    try:
        names, open_ids, closed = record_view(top, load_config(top))
    except (ConfigError, Refusal) as exc:
        return skipped('%s: %s' % (top, exc))
    got = orphans.scan(entries, names, open_ids, closed)
    return (['WARN %s' % w for w in got.warnings]
            + ['ruling orphans   %d Items line(s) read, %d WARN' % (got.subjects, len(got.warnings))])
