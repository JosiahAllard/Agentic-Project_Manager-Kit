"""kitlib.gate.land: the merge is made and gated in the landing tree (Lander.tree, a detached
linked worktree the kit owns), never in the main checkout. What the owner does in the main checkout
while the merged gate runs - a commit, a push, an edit, a stray file - reaches neither the gated tree
nor the pushed commit (docs/gate.md "Landing"). Each test acts on the main checkout from inside the
merged gate (merged_gate=, then the real kitlib.gate.runner.merged_gate)."""
import inspect
import os
import shutil
import tempfile
import unittest

from kitlib import gate as G
from kitlib.gate import runner
from tests.gate.test_land import BRANCH, LandCase


def real_gate(before=None, seen=None):
    """A merged_gate= that runs `before(lander)` (the owner at work in the main checkout), then the
    real merged gate of the landing, in its landing tree. `seen` (a list) gets the gated HEAD."""
    def fn(lander):
        if before is not None:
            before(lander)
        kw = dict(full=lander.full, slug='+'.join(lander.branches), log_dir=lander.log_dir,
                  say=lander.say, book_kw=lander.book_kw, slot=lander.slot)
        if 'tree' in inspect.signature(runner.merged_gate).parameters:
            kw['tree'] = lander.tree
        out = runner.merged_gate(lander.root, lander.cfg, lander.expect_main, **kw)
        if seen is not None:
            seen.append(out.facts.get('head'))
        return out
    return fn


class IsolatedCase(LandCase):
    def in_tree(self, commit, path):
        """True when `path` is in the tree of `commit`."""
        return self.repo.git('ls-tree', '--name-only', commit, '--', path) == path

    def landing_tree(self):
        return self.repo.path('.claude', 'worktrees', '_landing')


class OwnerAtWork(IsolatedCase):
    def test_a_commit_in_the_main_checkout_is_neither_gated_nor_pushed(self):
        # The owner commits RED on main while the gate runs: the step (red when RED is in its
        # tree) passes, the merge onto the base is pushed, and the owner's commit stays local.
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        made, seen = [], []

        def owner(lander):
            self.repo.write('RED', 'owner\n')
            made.append(self.repo.commit('owner work', 'RED'))
        out = self.lander(BRANCH, self.main, tip, merged_gate=real_gate(owner, seen)).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(seen, [out.merge])                 # the gate saw the merge, nothing else
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(self.repo.git('rev-parse', out.merge + '^1'), self.main)
        self.assertFalse(self.in_tree(out.merge, 'RED'))
        self.assertEqual(self.main_now(), made[0])          # the owner's commit is kept, not moved
        warn = [l for l in out.lines if l.startswith('WARNING') and 'merge --ff-only' in l]
        self.assertTrue(warn, out.lines)
        self.assertIn(made[0][:10], warn[0])
        self.assertIsNone(self.lock().read())

    def test_a_commit_pushed_during_the_gate_is_not_landed_over(self):
        # The owner commits and pushes main while the gate runs: the landing's push is refused
        # (never forced), the ungated merge never reaches the remote, and the lock is released.
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        made = []

        def owner(lander):
            self.repo.write('o.txt', 'owner\n')
            made.append(self.repo.commit('owner work', 'o.txt'))
            self.repo.git('push', '-q', 'origin', 'main')
        out = self.lander(BRANCH, self.main, tip, merged_gate=real_gate(owner)).run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.lines[-1].startswith('NOT LANDED'), out.lines)
        self.assertIn('nothing landed', out.lines[-1])
        self.assertEqual(self.remote_main(), made[0])
        self.assertNotIn(out.merge, self.repo.git('rev-list', made[0]).split())
        self.assertEqual(self.main_now(), made[0])
        self.assertIsNone(self.lock().read())
        self.assertFalse([l for l in out.lines if l.startswith('GATE OK')], out.lines)

    def test_a_tracked_edit_in_the_main_checkout_is_kept_and_not_gated(self):
        self.repo.write('notes.txt', 'one\n')
        self.main = self.repo.commit('notes', 'notes.txt')
        self.repo.git('push', '-q', 'origin', 'main')
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def owner(lander):
            self.repo.write('notes.txt', 'two\n')
        out = self.lander(BRANCH, self.main, tip, merged_gate=real_gate(owner)).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(self.main_now(), out.merge)        # fast-forwarded, the edit carried
        with open(self.repo.path('notes.txt'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'two\n')
        self.assertEqual(self.repo.git('show', '%s:notes.txt' % out.merge), 'one')
        self.assertIsNone(self.lock().read())

    def test_a_file_written_in_the_main_checkout_is_not_in_the_gated_tree(self):
        # RED in the main checkout's root: the step runs in the landing tree, which has none.
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def owner(lander):
            self.repo.write('RED', 'stray\n')
        out = self.lander(BRANCH, self.main, tip, merged_gate=real_gate(owner)).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(self.main_now(), out.merge)
        self.assertTrue(os.path.exists(self.repo.path('RED')))     # the owner's file is kept
        self.assertIsNone(self.lock().read())


class LandingTree(IsolatedCase):
    def land(self, name, **kw):
        _, tip = self.branch(name, {'%s.py' % name: 'x = 1\n'})
        said = []
        out = self.lander('kit/%s' % name, self.remote_main(), tip, say=said.append, **kw).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(self.main_now(), out.merge)
        return out, said

    def recreated(self, said):
        return [l for l in said if l.startswith('the landing tree ') and l.endswith(': recreated')]

    def test_the_tree_is_made_once_reused_and_recreated_when_dirty(self):
        tree = self.landing_tree()
        out, said = self.land('one')
        self.assertEqual(self.recreated(said), [])
        self.assertEqual(self.repo.head(tree), out.merge)       # left at the merge it gated
        listed = self.repo.git('worktree', 'list', '--porcelain')
        self.assertIn('locked', listed)
        self.repo.write('junk.txt', 'x\n', root=tree)
        with open(os.path.join(tree, 'one.py'), 'w', encoding='utf-8') as f:
            f.write('changed\n')
        out, said = self.land('two')
        self.assertEqual(len(self.recreated(said)), 1, said)
        self.assertIn(tree, self.recreated(said)[0])
        self.assertFalse(os.path.exists(os.path.join(tree, 'junk.txt')))
        self.assertEqual(self.repo.git('status', '--porcelain', cwd=tree), '')
        out, said = self.land('three')
        self.assertEqual(self.recreated(said), [])              # clean: reused as it is
        self.assertEqual(self.repo.head(tree), out.merge)

    def test_a_tree_deleted_by_hand_is_recreated(self):
        tree = self.landing_tree()
        self.land('one')
        shutil.rmtree(tree)
        out, said = self.land('two')
        self.assertEqual(len(self.recreated(said)), 1, said)
        self.assertEqual(self.repo.head(tree), out.merge)

    def test_a_folder_that_is_not_the_landing_tree_is_refused(self):
        tree = self.landing_tree()
        os.makedirs(tree)
        with open(os.path.join(tree, 'mine.txt'), 'w', encoding='utf-8') as f:
            f.write('keep me\n')
        _, tip = self.branch('one', {'one.py': 'x = 1\n'})
        out = self.lander('kit/one', self.main, tip).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertIn('move it aside', out.lines[-1])
        self.assertTrue(os.path.exists(os.path.join(tree, 'mine.txt')))
        self.assert_untouched(out, G.REFUSED)


class OtherDrive(IsolatedCase):
    def test_a_landing_tree_on_another_drive(self):
        other = os.path.join(os.environ.get('LOCALAPPDATA') or '', 'Temp')
        drive = os.path.splitdrive(os.path.abspath(other))[0].lower()
        if not os.environ.get('LOCALAPPDATA') or not os.path.isdir(other) or (
                drive == os.path.splitdrive(os.path.abspath(self.repo.root))[0].lower()):
            self.skipTest('no folder on a drive other than the test repository\'s (%s): %%LOCALAPPDATA%%'
                          '\\Temp is on the same drive or missing' % self.repo.root[:2])
        base = tempfile.mkdtemp(prefix='kit-land-tree-', dir=other)
        self.addCleanup(shutil.rmtree, base, True)
        tree = os.path.join(base, 'tree')
        self.addCleanup(self.repo.git, 'worktree', 'remove', '--force', '--force', tree, check=False)
        _, tip = self.branch('feature', {'RED': 'x\n'})
        # red first: the merge is gated in the tree on the other drive, and nothing is pushed
        out = self.lander(BRANCH, self.main, tip, tree=tree).run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(os.path.exists(os.path.join(tree, 'RED')))
        self.assertFalse(os.path.exists(self.repo.path('RED')))
        self.assert_untouched(out, G.FAILED)
        _, tip2 = self.branch('green', {'g.py': 'x = 1\n'})
        out = self.lander('kit/green', self.main, tip2, tree=tree).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(self.repo.head(tree), out.merge)


if __name__ == '__main__':
    unittest.main()
