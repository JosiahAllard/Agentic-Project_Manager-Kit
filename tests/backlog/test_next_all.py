"""`next --all` (docs/backlog.md, verb table): every startable record, not capped, with the same
pass-over lines `next` prints; `--all` beside `--cap` is USAGE."""
import unittest

from kitlib.backlog import offer as omod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod

from tests.backlog import test_cli
from tests.backlog.helpers import record_text

RULES = rmod.Rules('upper', ('L0', 'L1'))


def snapshot(*pairs):
    recs = [rmod.parse('backlog/%s.md' % rid, text, rid) for rid, text in pairs]
    return smod.Snapshot(recs, None, 'test', RULES)


def priced(rid, scope=None, **over):
    return rid, record_text(id=rid, priority='2', priority_by='agent',
                            scope=scope or 'area/%s/**' % rid.lower(), **over)


class NextAll(unittest.TestCase):

    def seven(self):
        return snapshot(*[priced('R%d' % i) for i in range(7)])

    def test_cap_none_offers_every_startable_record(self):
        (offers, passed), refusals, _ = omod.next_offer(self.seven(), [], cap=None)
        self.assertEqual(refusals, [])
        self.assertEqual([o.rec.id for o in offers], ['R%d' % i for i in range(7)])
        self.assertEqual(passed, [])

    def test_the_default_cap_still_caps(self):
        (offers, passed), _, _ = omod.next_offer(self.seven(), [])
        self.assertEqual(len(offers), omod.DEFAULT_CAP)
        self.assertEqual([p.why for p in passed], ['not reached - the cap was already full'] * 2)

    def test_all_keeps_the_collision_pass_over(self):
        snap = snapshot(priced('A1', scope='tools/x.py'), priced('A2', scope='tools/x.py'),
                        priced('A3'))
        (offers, passed), _, _ = omod.next_offer(snap, [], cap=None)
        self.assertEqual([o.rec.id for o in offers], ['A1', 'A3'])
        self.assertEqual([p.rec.id for p in passed], ['A2'])
        self.assertIn('already taken by A1 (offered above)', passed[0].why)


class NextAllCli(unittest.TestCase):
    """Through tools/backlog.py: the flag parses, lists past the cap, refuses beside --cap."""
    setUp = test_cli.Cli.setUp
    run_tool = test_cli.Cli.run_tool
    ok = test_cli.Cli.ok

    def test_next_all_lists_past_the_cap(self):
        for i in range(7):
            self.ok('add', 'R%d' % i, '--summary', 'item number %d' % i, '--scope', 'a/r%d.py' % i)
            self.ok('set', 'R%d' % i, '--priority', '2', '--priority-by', 'agent')
        out = self.ok('next', '--all')
        self.assertIn('NEXT (7 offered, --all: no cap)', out)
        self.assertNotIn('not reached', out)
        self.assertIn('NEXT (5 offered)', self.ok('next'))

    def test_all_with_cap_is_usage(self):
        rc, out = self.run_tool('next', '--all', '--cap', '3')
        self.assertEqual(rc, 2, out)
        self.assertIn('USAGE: ', out)


if __name__ == '__main__':
    unittest.main()
