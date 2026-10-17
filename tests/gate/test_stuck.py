"""The gate's third review round: a STUCK landing exits 3 whatever stopped it (Ctrl+C included),
and the commands a STUCK report prints fit what is left. Each clear-command test runs the printed
commands in the scratch repository and asserts the state they leave. Also: a merge interrupted
part way is undone.

The merge and its gate run in the landing tree (kitlib.gate.land, step 5b), so whatever stops a
landing before its push leaves nothing in the main checkout to undo: the lock is released and
the landing is NOT LANDED, never STUCK. STUCK is left for a push that may have landed.

Scratch repositories with a bare remote in temp and the scratch lock store only."""
import os
import subprocess
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import land
from kitlib.git import refs
from tests import testutil
from tests.gate.test_land import BRANCH, LandCase, main_only

STEP_CMD = '    $ '         # the documented prefix of a clear step's command line


def joined(out):
    return '\n'.join(out.lines)


def commands(out):
    return [l[len(STEP_CMD):] for l in out.lines if l.startswith(STEP_CMD)]


class _Stuck(LandCase):
    """Landings whose lock holder is a dead process, so the printed break command can break it."""

    def setUp(self):
        super().setUp()
        self.holder_process = testutil.dead_process()      # held until tearDown: the pid stays unused
        self.holder = self.holder_process.pid

    def lander(self, branch, expect_main, expect_tip, **kw):
        kw.setdefault('pid', self.holder)
        return LandCase.lander(self, branch, expect_main, expect_tip, **kw)

    def sh(self, command):
        r = subprocess.run(command, shell=True, cwd=self.tmp, env=self.env, capture_output=True,
                           text=True, encoding='utf-8', errors='replace')
        self.assertEqual(r.returncode, 0, '%s: %s %s' % (command, r.stdout, r.stderr))
        return r.stdout.strip()

    def assert_broken(self, printed):
        self.assertIn('True', printed)               # break_lock -> (True, the record)
        self.assertIsNone(self.lock().read())

    def head_branch(self):
        return self.repo.git('symbolic-ref', '--quiet', '--short', 'HEAD', check=False)


# ---------------------------------------------------------------- 1 STUCK is exit 3, Ctrl+C or not

class InterruptedStuck(_Stuck):
    def test_ctrl_c_after_a_verified_push_is_landed_not_stuck(self):
        # Once ls-remote shows the merge (phase pushed) the landing stands: Ctrl+C in the fetch
        # that follows is LANDED with a WARNING (exit 0), the stale tracking ref is set to the
        # verified merge and the lock released, so the next landing needs no hand step.
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        real = land.Lander.fetch

        def fetch(lander):
            if lander.phase == 'pushed':            # the push is on the remote, the tracking
                self.repo.git('update-ref', 'refs/remotes/origin/main', self.main)  # ref is not
                raise KeyboardInterrupt                                            # yet; Ctrl+C
            return real(lander)
        with mock.patch.object(land.Lander, 'fetch', fetch):
            out = self.lander(BRANCH, self.main, tip).run()    # returned, not raised (exit 0)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse([l for l in out.lines if l.startswith('STUCK')], out.lines)
        self.assertIn('WARNING: Ctrl+C after the verified push; the landing stands', out.lines)
        self.assertEqual(commands(out), [])
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(self.repo.git('rev-parse', 'refs/remotes/origin/main'), out.merge)
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.main_now(), out.merge)            # landed, and left landed


# ---------------------------------------------------------------- 2 clear commands fit the cause

class PushUnknown(_Stuck):
    def land_unreadable(self, tip):
        with mock.patch.object(refs, 'ls_remote', side_effect=main_only((None, 'network down'))):
            return self.lander(BRANCH, self.main, tip).run()

    def assert_fetch_first(self, out):
        self.assertEqual(out.code, G.STUCK, out.lines)
        self.assertIn('may have landed', joined(out))
        cmds = commands(out)
        self.assertIn(' fetch origin', cmds[0])
        self.assertIn(' rev-parse ', cmds[1])
        self.assertFalse(any(' reset ' in c for c in cmds[:2]), cmds)
        return cmds

    def test_a_push_that_landed_is_found_by_the_check_and_not_reset(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.land_unreadable(tip)
        cmds = self.assert_fetch_first(out)
        self.sh(cmds[0])
        self.assertEqual(self.sh(cmds[1]), out.merge)       # it landed: bring main to it
        self.assertEqual(len(cmds), 4, cmds)                # fetch, check, fast-forward, break
        self.assertIn(' merge --ff-only origin/main', cmds[2])
        self.sh(cmds[2])
        self.assert_broken(self.sh(cmds[-1]))
        self.assertEqual(self.main_now(), out.merge)
        self.assertEqual(self.remote_main(), out.merge)


# ---------------------------------------------------------------- 7 a merge interrupted part way

class HalfMerge(LandCase):
    def interrupt_merge(self, tip, in_progress):
        real = land.Lander.git

        def git(lander, *args, **kw):
            if args[:1] == ('merge',) and '--abort' not in args:
                if in_progress:                       # stopped before the merge commit
                    real(lander, 'merge', '--no-ff', '--no-commit', '-m', 'Merge', tip,
                         cwd=kw.get('cwd'))
                    trees.append(kw.get('cwd'))
                    self.assertTrue(refs.merge_in_progress(kw.get('cwd'))[0])   # stopped mid-merge
                else:                                 # stopped after it
                    real(lander, *args, **kw)
                raise KeyboardInterrupt
            return real(lander, *args, **kw)
        said, trees = [], []
        self.trees = trees
        with mock.patch.object(land.Lander, 'git', git):
            with self.assertRaises(KeyboardInterrupt):
                self.lander(BRANCH, self.main, tip, say=said.append).run()
        return said

    def assert_back(self):
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.remote_main(), self.main)
        self.assertEqual(self.repo.git('status', '--porcelain'), '')
        self.assertFalse(os.path.exists(os.path.join(self.repo.root, '.git', 'MERGE_HEAD')))
        self.assertIsNone(self.lock().read())

    def test_ctrl_c_with_the_merge_in_progress_aborts_it(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        said = self.interrupt_merge(tip, in_progress=True)
        self.assert_back()
        self.assertTrue(any('aborted' in s for s in said), said)
        tree = self.trees[0]                        # the merge was in the landing tree, and its
        self.assertTrue(tree and os.path.isdir(tree), self.trees)  # abort left nothing there
        merging, _ = refs.merge_in_progress(tree)
        self.assertFalse(merging)
        self.assertEqual(self.repo.git('-C', tree, 'status', '--porcelain'), '')


if __name__ == '__main__':
    unittest.main()
