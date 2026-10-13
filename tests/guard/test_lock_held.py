"""The guard's default lock_held (kitlib.guard.context) through kitlib.lock.run.nest_check on a
scratch store (tests/lock/helpers.StoreCase): MACHINE_LOCK_RUN counts only when it names a
holding run whose wrapper is an ancestor of this process. The variable alone is never proof."""
import os
import unittest

from .claudehost import decide

from tests.lock.helpers import StoreCase

from . import world as W
from .world import bash

if os.name == 'nt':
    from kitlib.proc import process

RID = 'r-20261006T120000Z-abcdef'


class DefaultLockHeld(StoreCase):
    def setUp(self):
        super().setUp()
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def ctx(self, value):
        return self.w.ctx(lock_held=None, store=self.store, environ={'MACHINE_LOCK_RUN': value})

    def parent_or_skip(self):
        anc = process.own_ancestors()
        pp = os.getppid()
        ct = process.proc_ctime(pp)
        if ct is None or (pp, ct) not in anc:
            self.skipTest('the parent process cannot be read as an ancestor')
        return pp, ct

    def test_the_variable_alone_is_not_proof(self):
        pp, ct = self.parent_or_skip()
        ctx = self.ctx('%s:%d:%d' % (RID, pp, ct))          # an ancestor, but no run record
        self.assertEqual(ctx.lock_held(), (False, 'the named run holds no lock'))
        self.assertEqual(decide(bash('godot --headless', self.w.wt), ctx).rule, 'heavy')

    def test_a_malformed_value(self):
        self.assertEqual(self.ctx('1').lock_held(), (False, 'malformed value'))

    def test_a_holding_ancestor_allows_heavy(self):
        pp, ct = self.parent_or_skip()
        self.live_record(RID, pid=pp, ctime=ct, has={'cpu': 2, 'gpu': 0, 'disk': 3}, cores=[0, 1])
        ctx = self.ctx('%s:%d:%d' % (RID, pp, ct))
        self.assertEqual(ctx.lock_held(), (True, None))
        self.assertIsNone(decide(bash('godot --headless', self.w.wt), ctx))


if __name__ == '__main__':
    unittest.main()
