"""lock-state (kitlib.guard.rules.lock_state) when the lock store's folder is a junction: the
store keeps its spelled path ~/.claude/machine-lock (kitlib.lock.store.real_root) and the owner
may move the folder to another volume with a junction left at the old path. A write spelled by the
junction's target is lock-state as much as one spelled through the junction, and so is one to the
target of a dangling junction. A folder beside the target is not. The fake home is world.World's;
the real lock store and ~/.claude are never touched."""
import os
import shutil
import unittest

from kitlib.guard import context

from . import world as W
from .claudehost import decide
from .world import bash, event

COORDINATOR = {context.ROLE_ENV: 'coordinator'}


class JunctionedStore(unittest.TestCase):

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.store = os.path.join(self.w.base, 'zstore', 'machine-lock')
        os.makedirs(self.store)
        os.makedirs(os.path.join(self.w.home, '.claude'))
        self.link = os.path.join(self.w.home, '.claude', 'machine-lock')
        try:
            import _winapi
            _winapi.CreateJunction(self.store, self.link)
        except (ImportError, AttributeError, OSError) as e:
            self.skipTest('no junction: %s' % e)
        # a junction is removed as a folder, never walked into
        self.addCleanup(lambda: os.path.lexists(self.link) and os.rmdir(self.link))

    def write(self, path, env=None, sub=False):
        return decide(event('Write', {'file_path': path, 'content': '{}'}, self.w.wt, sub),
                      self.w.ctx(environ=env or {}))

    def lock_state(self, d):
        self.assertIsNotNone(d, 'the write was let through')
        self.assertEqual((d.verdict, d.rule), ('deny', 'lock-state'), d.reason)

    def test_a_write_spelled_by_the_target_is_lock_state(self):
        self.lock_state(self.write(os.path.join(self.store, 'state.json'), COORDINATOR))
        self.lock_state(self.write(os.path.join(self.store, 'state.json'), sub=True))

    def test_a_shell_redirect_to_the_target_is_lock_state(self):
        target = W.fwd(os.path.join(self.store, 'state.json'))
        self.lock_state(decide(bash('echo {} > %s' % target, self.w.wt, sub=True), self.w.ctx()))

    def test_a_write_through_the_junction_is_lock_state(self):
        self.lock_state(self.write(os.path.join(self.link, 'state.json'), COORDINATOR))
        self.lock_state(self.write(os.path.join(self.link, 'state.json'), sub=True))

    def test_the_target_of_a_dangling_junction_is_still_lock_state(self):
        shutil.rmtree(self.store)
        self.assertTrue(os.path.lexists(self.link))
        self.lock_state(self.write(os.path.join(self.store, 'state.json'), COORDINATOR))
        self.lock_state(self.write(os.path.join(self.link, 'state.json'), COORDINATOR))

    def test_beside_the_target_is_not_lock_state(self):
        for name in ('machine-lock-notes.md', os.path.join('machine-lock-old', 'state.json')):
            with self.subTest(name):
                d = self.write(os.path.join(os.path.dirname(self.store), name), COORDINATOR)
                self.assertNotEqual(d and d.rule, 'lock-state', d and d.reason)


if __name__ == '__main__':
    unittest.main()
