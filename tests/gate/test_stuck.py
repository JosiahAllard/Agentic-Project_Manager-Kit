"""The gate's third review round: a STUCK landing exits 3 whatever stopped it (Ctrl+C included),
and the commands a STUCK report prints fit what is left. Each clear-command test runs the printed
commands in the scratch repository and asserts the state they leave. Also: a green merged gate
that is not landed prints no bare `GATE OK`, and a merge interrupted part way is undone.

The merge and its gate run in the landing tree (kitlib.gate.land, step 5b), so whatever stops a
landing before its push leaves nothing in the main checkout to undo: the lock is released and
the landing is NOT LANDED, never STUCK. STUCK is left for a push that may have landed.

Scratch repositories with a bare remote in temp and the scratch lock store only."""
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import land
from kitlib.git import refs
from tests import testutil
from tests.gate import test_hardening as H
from tests.gate.test_land import BRANCH, LandCase, main_only
from tests.realgit import real_git

STEP_CMD = '    $ '         # the documented prefix of a clear step's command line
VERDICT = re.compile(r'^GATE (OK|FAILED)\b')


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

    def assert_released_and_kept(self):
        """Nothing was pushed, the lock is released (no hand step), and the main checkout was
        never merged into: still on main at the base, its own edit kept."""
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.remote_main(), self.main)
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.head_branch(), 'main')
        with open(self.repo.path('README.md'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'changed by the gate\n')

    def test_ctrl_c_during_the_gate_is_released_and_raised_not_stuck(self):
        # An edit in the main checkout while the merged gate ran is the owner's, not the gate's:
        # the gate runs in the landing tree, so there is no undo to verify, and Ctrl+C is Ctrl+C
        # (exit 130) once the lock is released.
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def edits_then_interrupted(lander):
            self.repo.write('README.md', 'changed by the gate\n')
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.lander(BRANCH, self.main, tip, merged_gate=edits_then_interrupted).run()
        self.assert_released_and_kept()

    def test_system_exit_during_the_gate_is_not_landed_not_stuck(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def edits_then_exits(lander):
            self.repo.write('README.md', 'changed by the gate\n')
            raise SystemExit(0)
        out = self.lander(BRANCH, self.main, tip, merged_gate=edits_then_exits).run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertIn('SystemExit', out.lines[0])
        self.assertTrue(out.lines[0].startswith('NOT LANDED'), out.lines)
        self.assertEqual(commands(out), [])
        self.assert_released_and_kept()


# ---------------------------------------------------------------- 2 clear commands fit the cause

class HeadOnAnotherBranch(_Stuck):
    def test_a_switch_in_the_main_checkout_lands_and_never_moves_the_other_branch(self):
        # The gated merge is in the landing tree, so a switch in the main checkout changes
        # nothing the push depends on: it lands, and the main checkout, on another branch, is
        # not fast-forwarded (a WARNING says to switch back first).
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def switches(lander):
            self.repo.git('switch', '-q', '-c', 'elsewhere')
            return G.Outcome(G.OK, ['GATE OK'], verdict='GATE OK', results=[])
        out = self.lander(BRANCH, self.main, tip, merged_gate=switches).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIn('switch back', joined(out))
        self.assertEqual(commands(out), [])
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.head_branch(), 'elsewhere')                          # untouched
        self.assertEqual(self.repo.git('rev-parse', 'refs/heads/elsewhere'), self.main)
        self.assertEqual(self.main_now(), self.main)                    # not fast-forwarded
        self.assertEqual(self.remote_main(), out.merge)


class _Mode(H._CommittedTools):
    """Landings whose merged gate runs tools/mode_step.py (what the branch's MODE file says)."""
    GATE = H.GatedIsPushed.GATE
    FILES = H.GatedIsPushed.FILES

    def land_mode(self, mode):
        _, tip = self.branch('feature', {'MODE': mode + '\n'})
        return self.lander(BRANCH, self.main, tip).run()


class CommitsBeyondTheMerge(_Stuck, _Mode):
    def test_extra_commits_are_named_and_never_reach_main(self):
        _, tip = self.branch('feature', {'MODE': 'commit\n'})
        lander = self.lander(BRANCH, self.main, tip)
        out = lander.run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        extra = lander.tree_head()                  # the commit the step made, in the tree
        self.assertNotEqual(extra, lander.merge)
        self.assertIn(extra[:10], joined(out))
        self.assertEqual(self.repo.git('log', '-1', '--format=%s', extra), 'made by the gate')
        self.assertEqual(commands(out), [])         # nothing to clear by hand
        self.assertIsNone(self.lock().read())
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.remote_main(), self.main)


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

    def test_a_remote_main_neither_base_nor_merge_is_checked_and_nothing_reset(self):
        # The tracking ref is known not to hold the merge; what the fetch shows may be neither
        # the merge nor the base (someone else pushed on top of it), so the remote branches
        # holding the merge are listed. The main checkout was never merged into: no step resets
        # or keeps anything, whatever the check shows.
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        real = land.Lander.on_remote

        def on_remote(lander, pushed):
            if sys._getframe(1).f_code.co_name == 'clear_steps':
                return False
            return real(lander, pushed)
        with mock.patch.object(land.Lander, 'on_remote', on_remote):
            out = self.land_unreadable(tip)
        cmds = self.assert_fetch_first(out)
        self.assertIn('if it printed anything else', joined(out))
        contains = [i for i, c in enumerate(cmds) if ' branch -r --contains %s' % out.merge in c]
        undo = [i for i, c in enumerate(cmds) if ' reset ' in c or ' branch kit-stuck' in c]
        self.assertEqual(len(contains), 1, cmds)
        self.assertEqual(undo, [], cmds)
        self.assertIn('nothing is to be undone', joined(out))
        self.assertIn('break_lock()', cmds[-1])


@real_git('pre-receive hook')
class PushUnknownRejected(_Stuck):
    """Moved out of PushUnknown for the real-git tier: on the fake the hook made the push raise
    Unsupported, which land.recover() turned into STUCK, so it passed for the wrong reason."""

    land_unreadable = PushUnknown.land_unreadable
    assert_fetch_first = PushUnknown.assert_fetch_first

    def test_a_push_that_did_not_land_is_found_by_the_check_and_reset(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        hook = os.path.join(self.repo.remote, 'hooks', 'pre-receive')
        with open(hook, 'w', newline='\n') as f:
            f.write('#!/bin/sh\necho rejected by test >&2\nexit 1\n')
        out = self.land_unreadable(tip)
        cmds = self.assert_fetch_first(out)
        self.sh(cmds[0])
        self.assertEqual(self.sh(cmds[1]), self.main)       # it did not land: go on
        for c in cmds[2:]:
            printed = self.sh(c)
        self.assert_broken(printed)
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.remote_main(), self.main)


# ---------------------------------------------------------------- 4 no bare GATE OK when not landed

class GreenNotLanded(_Mode):
    def test_a_green_gate_that_is_not_landed_prints_no_gate_ok_line(self):
        out = self.land_mode('untracked')
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertEqual(out.verdict, 'GATE OK')                  # the merged gate's own verdict
        self.assertFalse([l for l in out.lines if VERDICT.match(l)], out.lines)
        self.assertTrue(any(l.startswith('merged gate passed') for l in out.lines), out.lines)
        self.assertTrue(any(l.startswith('NOT LANDED:') for l in out.lines), out.lines)

    def test_a_landed_landing_still_prints_gate_ok(self):
        out = self.land_mode('plain')
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(VERDICT.match(out.lines[0]), out.lines)


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

    def test_ctrl_c_after_the_merge_commit_undoes_it(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        said = self.interrupt_merge(tip, in_progress=False)
        self.assert_back()
        self.assertTrue(any('undone' in s for s in said), said)


if __name__ == '__main__':
    unittest.main()
