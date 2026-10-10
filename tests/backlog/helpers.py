"""Shared fixtures for the backlog tests: a record builder in the per-repository shape and a store
on a real temporary repository with a bare remote (tests/gitfixture.py)."""
import os
import tempfile
import unittest
from unittest import mock

from kitlib.backlog import ledger as lmod
from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from tests.gitfixture import TempRepo, fixture_env, rmtree

BASE = [
    ('id', 'FOO'),
    ('area', 'wg'),
    ('priority', '-'),
    ('priority_by', '-'),
    ('effort', '-'),
    ('state', 'open'),
    ('blocked_on', '-'),
    ('scope', 'worldgraph/**'),
    ('summary', 'a thing that is wrong and should be fixed'),
]
BRANCH = 'tools/some-branch'
TODAY = '2026-10-07'


def record_text(drop=(), body='', **over):
    """A record's text: the minimal valid header, keys dropped or overridden; a list value writes
    one line per member."""
    pairs = []
    for key, value in BASE:
        if key in drop:
            over.pop(key, None)
            continue
        if key in over:
            value = over.pop(key)
        pairs.append((key, value))
    pairs.extend(over.items())
    lines = []
    for key, value in pairs:
        for one in (value if isinstance(value, (list, tuple)) else [value]):
            lines.append('%s: %s' % (key, one))
    text = '\n'.join(lines) + '\n'
    if body:
        text += '\n' + body + '\n'
    return text


def rec(rid, **over):
    """{stem: text} for one record with `id` set and the scope defaulted per id (no collisions)."""
    over.setdefault('scope', 'area/%s/**' % rid.lower())
    return {rid: record_text(id=rid, **over)}


def priced(rid, priority='2', **over):
    over.setdefault('priority_by', 'agent')
    return rec(rid, priority=priority, **over)


def parse(stem='FOO', **kw):
    return rmod.parse('backlog/%s.md' % stem, record_text(**kw), stem=stem)


def messages(stem='FOO', rules=None, **kw):
    r = parse(stem=stem, **kw)
    return [f.message for f in r.structural + rmod.validate(r, rules or rmod.Rules())]


LEDGER_ITEMS = 'Items: FOO, BAR, BAZ, QUX\n'
STANDING = 'R-9008'                 # the fixture's standing delegation (machine config standing_delegation)
LEDGER_TEXT = ('# Rulings ledger (test fixture)\n\n'
               '## R-9001 | 2026-09-25 08:00Z | kit | typed\n\nstand-in text\n' + LEDGER_ITEMS + '\n'
               '## R-9002 | 2026-09-25 08:01Z | all | selected\n\nstand-in text\n' + LEDGER_ITEMS + '\n'
               '## R-9003 | 2026-09-25 08:02Z | beta | typed\n\nstand-in text\n' + LEDGER_ITEMS + '\n'
               '## R-9004 | 2026-09-25 08:03Z | kit | delegated\n\nstand-in text\n' + LEDGER_ITEMS + '\n'
               '## R-9005 | 2026-09-25 08:04Z | kit | typed\n\nno items line here\n\n'
               '## R-9006 | 2026-09-25 08:05Z | kit | pm-note\n\nstand-in text\n' + LEDGER_ITEMS + '\n'
               '## R-9007 | 2026-09-25 08:06Z | kit | typed\n\n'
               'Retired: superseded - replaced (see R-9001)\n\n'
               '## R-9008 | 2026-09-25 08:08Z | all | selected\n\nstand-in text\n' + LEDGER_ITEMS)


def _branch(repo):
    """The template's prepare: BRANCH at main (the plan context's branch)."""
    repo.git('branch', BRANCH)


class StoreCase(unittest.TestCase):
    """A ref store on a temp repository with a bare remote (`remote = False`: none)."""
    remote = True
    object_format = None
    ledger_text = None
    standing = STANDING

    def setUp(self):
        # The library's own git calls inherit os.environ: no global config, no auto-gc/maintenance.
        patch = mock.patch.dict(os.environ, fixture_env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.repo = TempRepo(bare_remote=self.remote, object_format=self.object_format,
                             prepare=_branch, prepare_key='backlog.helpers.StoreCase')
        self.addCleanup(self.repo.cleanup)
        self.rules = rmod.Rules('upper', ('L0', 'L1'))
        self.backend = self.make_backend()
        self.store = smod.Store(self.backend, self.rules)
        path = None
        if self.ledger_text is not None:
            d = tempfile.mkdtemp(prefix='kit-ledger-')
            self.addCleanup(rmtree, d)
            path = os.path.join(d, 'ledger.md')
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(self.ledger_text)
        self.ledger = lmod.Ledger(path, ['kit'], self.standing)
        self.ctx = pmod.Context(self.store, self.ledger, self.repo.root, branch=BRANCH, today=TODAY)

    def make_backend(self, root=None):
        return smod.RefBackend(root or self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)

    def seed(self, *records):
        writes = {}
        for r in records:
            writes.update(r)
        snap = self.store.load()
        return self.backend.publish(snap, writes, 'seed\n')

    def verb(self, name, *args, **kw):
        """Run plan_<name> through Store.write; returns the Change."""
        plan = getattr(pmod, 'plan_' + name)
        change, _tip = self.store.write(lambda s: plan(s, self.ctx, *args, **kw))
        return change

    def get(self, rid):
        return self.store.load().get(rid)

    def remote_tip(self):
        if not self.remote:
            return self.repo.git('rev-parse', '--verify', '--quiet', smod.STORE_REF, check=False)
        out = self.repo.git('ls-remote', 'origin', smod.STORE_REF)
        return out.split('\t')[0] if out else ''
