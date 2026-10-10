"""The gate's third review round: a STUCK landing exits 3 whatever stopped it (Ctrl+C included),
and the commands a STUCK report prints fit what is left. Each clear-command test runs the printed
commands in the scratch repository and asserts the state they leave. Also: a green merged gate
that is not landed prints no bare `GATE OK`, and a merge interrupted part way is undone.

Scratch repositories with a bare remote in temp and the scratch lock store only."""
import os
import re
import subprocess
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

    def test_ctrl_c_when_the_undo_cannot_verify_is_stuck_not_130(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def edits_then_interrupted(lander):
            self.repo.write('README.md', 'changed by the gate\n')
            raise KeyboardInterrupt
        out = self.lander(BRANCH, self.main, tip, merged_gate=edits_then_interrupted).run()
        self.assertEqual(out.code, G.STUCK, out.lines)
        self.assertIn('Ctrl+C', out.lines[0])
        self.assertEqual(self.lock().read()['phase'], 'stuck')
        self.assertEqual(self.remote_main(), self.main)
        for c in commands(out):                     # the tracked change is kept by reset --keep
            printed = self.sh(c)
        self.assert_broken(printed)
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.head_branch(), 'main')
        with open(self.repo.path('README.md'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'changed by the gate\n')

    def test_system_exit_when_the_undo_cannot_verify_is_stuck(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def edits_then_exits(lander):
            self.repo.write('README.md', 'changed by the gate\n')
            raise SystemExit(0)
        out = self.lander(BRANCH, self.main, tip, merged_gate=edits_then_exits).run()
        self.assertEqual(out.code, G.STUCK, out.lines)
        self.assertIn('SystemExit', out.lines[0])


# ---------------------------------------------------------------- 2 clear commands fit the cause

class HeadOnAnotherBranch(_Stuck):
    def test_switch_back_first_and_never_reset_the_other_branch(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def switches(lander):
            self.repo.git('switch', '-q', '-c', 'elsewhere')
            return G.Outcome(G.OK, ['GATE OK'], verdict='GATE OK', results=[])
        out = self.lander(BRANCH, self.main, tip, merged_gate=switches).run()
        self.assertEqual(out.code, G.STUCK, out.lines)
        self.assertIn('switch back', joined(out))
        cmds = commands(out)
        switch = [i for i, c in enumerate(cmds) if ' switch main' in c]
        resets = [i for i, c in enumerate(cmds) if ' reset ' in c]
        self.assertEqual(len(switch), 1, cmds)
        self.assertTrue(all(i > switch[0] for i in resets), cmds)
        for c in cmds:
            printed = self.sh(c)
        self.assert_broken(printed)
        self.assertEqual(self.head_branch(), 'main')
        self.assertEqual(self.main_now(), self.main)
        self.assertEqual(self.repo.git('rev-parse', 'refs/heads/elsewhere'), out.merge)  # untouched
        self.assertEqual(self.remote_main(), self.main)


class _Mode(H._CommittedTools):
    """Landings whose merged gate runs tools/mode_step.py (what the branch's MODE file says)."""
    GATE = H.GatedIsPushed.GATE
    FILES = H.GatedIsPushed.FILES

    def land_mode(self, mode):
        _, tip = self.branch('feature', {'MODE': mode + '\n'})
        return self.lander(BRANCH, self.main, tip).run()


class CommitsBeyondTheMerge(_Stuck, _Mode):
    def test_extra_commits_are_named_and_kept_before_the_reset(self):
        _, tip = self.branch('feature', {'MODE': 'commit\n'})
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.STUCK, out.lines)
        extra = self.main_now()
        self.assertNotEqual(extra, out.merge)
        self.assertIn('made by the gate', joined(out))
        self.assertIn(extra[:10], joined(out))
        cmds = commands(out)
        keep = [i for i, c in enumerate(cmds) if ' branch ' in c]
        reset = [i for i, c in enumerate(cmds) if ' reset ' in c]
        self.assertEqual(len(keep), 1, cmds)
        self.assertTrue(reset and keep[0] < reset[0], cmds)
        for c in cmds:
            printed = self.sh(c)
        self.assert_broken(printed)
        self.assertEqual(self.main_now(), self.main)
        branch = cmds[keep[0]].split(' branch ')[1].split()[0]
        self.assertEqual(self.repo.git('rev-parse', 'refs/heads/%s' % branch), extra)  # kept


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
        self.assertEqual(self.sh(cmds[1]), out.merge)       # it landed: skip to the last step
        self.assert_broken(self.sh(cmds[-1]))
        self.assertEqual(self.main_now(), out.merge)
        self.assertEqual(self.remote_main(), out.merge)


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
                    real(lander, 'merge', '--no-ff', '--no-commit', tip)
                else:                                 # stopped after it
                    real(lander, *args, **kw)
                raise KeyboardInterrupt
            return real(lander, *args, **kw)
        said = []
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

    def test_ctrl_c_after_the_merge_commit_undoes_it(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        said = self.interrupt_merge(tip, in_progress=False)
        self.assert_back()
        self.assertTrue(any('undone' in s for s in said), said)


if __name__ == '__main__':
    unittest.main()
