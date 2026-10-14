"""The stash guard (kitlib.gate.stash; docs/gate.md "The stash guard"): the branch gate and a
landing refuse while the repository's stash stack holds an entry, whichever checkout made it, and
an entry that appears while a gate runs costs the branch gate its gate-OK record and undoes a
landing's merge.

A stash entry here is the ref refs/stash, set with `git update-ref` in the scratch repository: no
test runs `git stash push`. Scratch repositories with a bare remote in temp and the scratch lock
store only."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import result, runner, submit
from kitlib.git import refs
from kitlib.records import gate_ok
from tests.gate import fixtures
from tests.gate.test_land import BRANCH, HANG_S, KIT_ROOT, LandCase
from tests.gate.test_land_submit import GATE_PY, SubmitCase
from tests.realgit import real_git

REF = 'refs/stash'
GREEN = G.Outcome(G.OK, ['GATE OK'], verdict='GATE OK', results=[])
# A step that puts an entry on the stash stack of the repository it runs in. It is a python that
# runs git: the gate starts steps in this process, where the unit tier allows no git. It prints,
# because a step with an empty log is a run that never happened.
STASHER = [sys.executable, '-c',
           'import subprocess; subprocess.check_call(["git", "update-ref", "%s", "HEAD"]); '
           'print("ok")' % REF]


def joined(out):
    return '\n'.join(out.lines)


def unreadable(why='git said no'):
    """Patches kitlib.git.refs.ref_commit so that git cannot tell whether refs/stash exists; every
    other ref gets git's answer."""
    real = refs.ref_commit

    def ref_commit(ref, cwd):
        if ref == REF:
            return None, why
        return real(ref, cwd)
    return mock.patch.object(refs, 'ref_commit', ref_commit)


class _Stack:
    """Mixed into a GateRepoCase: the scratch repository's stash stack."""

    def stash_entry(self, commit=None, cwd=None):
        self.repo.git('update-ref', REF, commit or self.main, cwd=cwd)

    def drop_entry(self, cwd=None):
        self.repo.git('update-ref', '-d', REF, cwd=cwd)

    def gate(self, wt, **kw):
        kw.setdefault('log_dir', self.logs)
        return runner.branch_gate(wt, book_kw=self.book_kw, **kw)

    def assert_refused_and_nothing_ran(self, out, wt, tip):
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertTrue(out.lines[0].startswith('REFUSED: the stash stack is not empty'), out.lines)
        # the refusal, then only the source line runner._branch_gate ends every outcome with
        # once the config is read (kitlib.gate.detect.label)
        self.assertEqual(out.lines[1:], ['source: gate.json'], out.lines)
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, tip)))
        self.assertEqual(self.store.history_records(1), [])          # nothing booked or run
        self.assertFalse(os.path.exists(self.logs))


class BranchGateRefuses(_Stack, fixtures.GateRepoCase):
    def test_a_stash_entry_refuses_the_gate_and_nothing_runs(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.stash_entry()
        out = self.gate(wt)
        self.assert_refused_and_nothing_ran(out, wt, tip)
        self.assertIn(self.main[:10], out.lines[0])                   # where refs/stash is
        self.assertIn('git stash list', out.lines[0])                 # how to see the entries

    def test_an_entry_made_in_another_checkout_refuses_every_checkout(self):
        """The stack is the repository's: an entry made from one linked worktree refuses the gate
        of another, and of the main checkout."""
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        other, other_tip = self.branch('other', {'b.py': 'y = 1\n'})
        self.stash_entry(other_tip, cwd=other)
        self.assert_refused_and_nothing_ran(self.gate(wt), wt, tip)
        self.assert_refused_and_nothing_ran(self.gate(other), other, other_tip)
        self.assert_refused_and_nothing_ran(self.gate(self.repo.root), self.repo.root, self.main)

    def test_plan_only_and_full_are_refused_too(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.stash_entry()
        for kw in ({'plan': True}, {'only': ['unit']}, {'full': True}):
            with self.subTest(kw=kw):
                self.assert_refused_and_nothing_ran(self.gate(wt, **kw), wt, tip)

    def test_the_gate_runs_again_once_the_entry_is_gone(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.stash_entry()
        self.assertEqual(self.gate(wt).code, G.REFUSED)
        self.drop_entry()
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(out.record, gate_ok.record_path(wt, tip))

    def test_a_stack_git_cannot_read_is_refused(self):
        """Fail closed: no answer from git about refs/stash is not an empty stack."""
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        with unreadable('git said no'):
            out = self.gate(wt)
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertTrue(out.lines[0].startswith('REFUSED: git cannot tell whether the stash stack '
                                                'is empty: git said no'), out.lines)
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, tip)))
        self.assertEqual(self.store.history_records(1), [])
        self.assertFalse(os.path.exists(self.logs))

    def test_an_entry_made_while_the_gate_ran_writes_no_record(self):
        """The tree is clean before and after and HEAD did not move, yet a tree that was stashed
        while the steps ran is not the tree the commit names: no gate-OK record."""
        wt = self.repo.worktree('feature')
        step = {'name': 'stasher', 'always': True, 'cmd': STASHER}
        self.repo.write('.kit.json', '{"gate": {"steps": [%s]}}\n' % json.dumps(step), root=wt)
        tip = self.repo.commit('stasher gate', '.kit.json', cwd=wt)
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.repo.git('rev-parse', REF, cwd=wt), tip)     # the step did run
        self.assertEqual(self.repo.git('status', '--porcelain', cwd=wt), '')
        self.assertIsNone(out.record)
        self.assertIn('the stash stack is not empty', out.no_record)
        self.assertIn('while the gate ran', out.no_record)
        self.assertIn('no gate-OK record: %s' % out.no_record, out.lines)
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, tip)))

    def test_a_stack_git_cannot_read_after_the_run_writes_no_record(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        real, calls = refs.ref_commit, []

        def ref_commit(ref, cwd):
            if ref == REF:
                calls.append(ref)
                if len(calls) > 1:              # the read before the run answers; later ones do not
                    return None, 'git said no'
            return real(ref, cwd)
        with mock.patch.object(refs, 'ref_commit', ref_commit):
            out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(len(calls), 2, calls)
        self.assertIsNone(out.record)
        self.assertIn('git cannot tell whether the stash stack is empty: git said no', out.no_record)
        self.assertFalse(os.path.exists(gate_ok.record_path(wt, tip)))


class LandingRefuses(_Stack, LandCase):
    def test_a_stash_entry_refuses_the_landing_before_any_merge(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.stash_entry()
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertTrue(out.lines[0].startswith('REFUSED: the stash stack is not empty'), out.lines)
        self.assertTrue(out.lines[0].endswith('; nothing was merged'), out.lines)
        self.assert_untouched(out, G.REFUSED)
        # the landing's one turn (booked before the locked checks, since the landing queue) and
        # given back; the merged gate, which runs in that slot, never ran (no GATE verdict line)
        self.assertEqual([c.get('priority') for c in self.book_calls], ['land'], self.book_calls)
        self.assertEqual([r['slug'] for r in self.store.history_records(1)], ['kit-feature'])
        self.assertFalse([l for l in out.lines if l.startswith('GATE ')], out.lines)

    def test_an_entry_made_in_a_linked_worktree_refuses_the_landing(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.stash_entry(tip, cwd=wt)
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertIn('the stash stack is not empty', out.lines[0])
        self.assert_untouched(out, G.REFUSED)

    def test_a_stack_git_cannot_read_refuses_the_landing(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        with unreadable('git said no'):
            out = self.lander(BRANCH, self.main, tip).run()
        self.assertTrue(out.lines[0].startswith('REFUSED: git cannot tell whether the stash stack '
                                                'is empty: git said no'), out.lines)
        self.assert_untouched(out, G.REFUSED)

    def test_the_landing_goes_through_once_the_entry_is_gone(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.stash_entry()
        self.assertEqual(self.lander(BRANCH, self.main, tip).run().code, G.REFUSED)
        self.drop_entry()
        out = self.lander(BRANCH, self.main, tip).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        # the control for the refusal's check: a merged gate that ran prints its GATE verdict
        self.assertIn('GATE OK', out.lines)

    def test_an_entry_made_while_the_merged_gate_ran_undoes_the_merge(self):
        """A green merged gate, a clean main checkout at the merge, and a stash entry that was not
        there when the landing started: nothing is pushed, and the merge is undone."""
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        seen = []

        def gate(lander):
            seen.append(self.main_now())
            self.stash_entry(self.main)
            return GREEN
        out = self.lander(BRANCH, self.main, tip, merged_gate=gate).run()
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], self.main)                       # the merge was there, gated
        self.assertIn('NOT LANDED: the merged gate passed, and the stash stack is not empty',
                      joined(out))
        self.assertIn('while the gate ran', joined(out))
        self.assertIn('nothing was pushed', joined(out))
        self.assertTrue(out.undone)
        self.assert_untouched(out, G.FAILED)
        self.assertFalse(os.path.exists(self.repo.path('a.py')))

    def test_a_stack_git_cannot_read_after_the_merged_gate_undoes_the_merge(self):
        """Fail closed after the run too: a green merged gate, then no answer from git about
        refs/stash, is NOT LANDED and the merge undone, never an empty stack."""
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        real, broken, calls = refs.ref_commit, [], []

        def ref_commit(ref, cwd):
            if ref == REF:
                calls.append(bool(broken))
                if broken:                      # the reads after the merged gate get no answer
                    return None, 'git said no'
            return real(ref, cwd)

        def gate(lander):
            broken.append(True)
            return GREEN
        with mock.patch.object(refs, 'ref_commit', ref_commit):
            out = self.lander(BRANCH, self.main, tip, merged_gate=gate).run()
        self.assertEqual(calls, [False, True], calls)   # read before the merge, and after the gate
        self.assertIn('NOT LANDED: the merged gate passed, and git cannot tell whether the stash '
                      'stack is empty: git said no', joined(out))
        self.assertIn('nothing was pushed', joined(out))
        self.assertTrue(out.undone)
        self.assert_untouched(out, G.FAILED)
        self.assertFalse(os.path.exists(self.repo.path('a.py')))


class SubmittedLandingRefuses(_Stack, SubmitCase):
    """A --submit landing (kitlib.gate.submit) with an entry on the stack: the submitter, which
    checks nothing, prints SUBMITTED and exits 0; the detached landing refuses before any merge,
    and the refusal is its final land result record, which wait-result prints."""

    def test_the_refusal_reaches_wait_result(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.stash_entry()
        sub_out, _ = self.submit([BRANCH], [tip], expect_main=self.main)
        self.assertEqual(sub_out.code, G.OK, sub_out.lines)
        self.assertTrue(sub_out.lines[0].startswith('SUBMITTED kit/feature'), sub_out.lines)
        running = self.record(BRANCH)
        self.assertEqual(running['verdict'], 'running')
        # what the detached tool runs (tests/gate/test_land_submit.py TheDetachedLanding)
        sub = submit.restore(self.repo.root, sub_out.facts['payload'])
        out = self.lander(BRANCH, self.main, tip, submitted=sub).run()
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertTrue(out.lines[0].startswith('REFUSED: the stash stack is not empty'), out.lines)
        self.assert_untouched(out, G.REFUSED)

        rec, why = result.wait(self.repo.root, BRANCH, tip, kind='land', timeout_s=0)
        self.assertIsNone(why)
        self.assertEqual((rec['run_id'], rec['verdict'], rec['code']),
                         (running['run_id'], 'refused', G.REFUSED))
        self.assertTrue(rec['summary'].startswith('REFUSED: the stash stack is not empty'), rec)
        self.assertIsNotNone(rec['finished_at'])

        r = subprocess.run([sys.executable, GATE_PY, 'wait-result', BRANCH, '--tip', tip,
                            '--kind', 'land', '--timeout', '0', '-C', self.repo.root],
                           capture_output=True, text=True, timeout=HANG_S, env=self.env,
                           stdin=subprocess.DEVNULL, cwd=KIT_ROOT)
        self.assertEqual(r.returncode, G.FAILED, (r.stdout, r.stderr))    # any verdict but pass
        first = r.stdout.splitlines()[0]
        self.assertTrue(first.startswith('RESULT refused land kit/feature at %s: REFUSED: the '
                                         'stash stack is not empty' % tip[:10]), r.stdout)


class EmptyStack(_Stack, LandCase):
    """Guards: with an empty stack nothing is refused, and the fixture's injected green merged
    gate lands (so the undo test above fails for the stash entry, not for its gate)."""

    def test_an_empty_stack_gates_and_writes_the_record(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.assertEqual(self.gate(wt, plan=True).code, G.OK)
        out = self.gate(wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertIsNone(out.no_record)
        self.assertEqual(out.record, gate_ok.record_path(wt, tip))

    def test_an_empty_stack_lands_with_the_injected_green_gate(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        out = self.lander(BRANCH, self.main, tip, merged_gate=lambda lander: GREEN).run()
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(self.remote_main(), out.merge)


class StackRead(_Stack, fixtures.GateRepoCase):
    """kitlib.gate.stash itself: what it reads and what it says."""

    def setUp(self):
        fixtures.GateRepoCase.setUp(self)
        from kitlib.gate import stash        # absent before the guard: each test here then errors
        self.stash = stash

    def test_an_empty_stack_is_no_refusal(self):
        wt = self.repo.worktree('feature')
        for where in (self.repo.root, wt):
            self.assertEqual(self.stash.tip(where), ('', None))
            self.assertIsNone(self.stash.refusal(where))
            self.assertIsNone(self.stash.appeared(where))

    def test_an_entry_is_seen_from_every_checkout(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.stash_entry(tip, cwd=wt)
        for where in (self.repo.root, wt):
            self.assertEqual(self.stash.tip(where), (tip, None))
            why = self.stash.refusal(where)
            self.assertTrue(why.startswith('the stash stack is not empty (%s is at %s)'
                                           % (REF, tip[:10])), why)
            self.assertIn('git stash list', why)
            self.assertNotIn('REFUSED', why)         # the caller words the refusal
            self.assertNotIn('\n', why)
            late = self.stash.appeared(where)
            self.assertTrue(late.startswith('the stash stack is not empty (%s is at %s)'
                                            % (REF, tip[:10])), late)
            self.assertIn('while the gate ran', late)

    def test_no_answer_from_git_is_a_refusal(self):
        said = 'git cannot tell whether the stash stack is empty: git said no'
        with unreadable('git said no'):
            self.assertEqual(self.stash.tip(self.repo.root), (None, 'git said no'))
            self.assertEqual(self.stash.refusal(self.repo.root), said)
            self.assertEqual(self.stash.appeared(self.repo.root), said)

    def test_not_a_repository_is_a_refusal(self):
        self.assertIsNotNone(self.stash.refusal(self.tmp))


@real_git('stash: the fake has no `git stash`, so only real git shows that the ref the tests set '
          'is an entry `git stash list` prints')
class RealStack(_Stack, fixtures.GateRepoCase):
    def test_the_entry_git_stash_list_prints_refuses_a_linked_worktree(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.assertEqual(self.repo.git('stash', 'list', cwd=wt), '')
        self.assertEqual(self.gate(wt, plan=True).code, G.OK)
        self.repo.git('update-ref', '--create-reflog', '-m', 'WIP on main: a test entry', REF,
                      self.main)
        listed = self.repo.git('stash', 'list', cwd=wt)                # read-only, from the worktree
        self.assertEqual(listed, 'stash@{0}: WIP on main: a test entry')
        out = self.gate(wt)
        self.assert_refused_and_nothing_ran(out, wt, tip)
        self.assertIn(self.main[:10], out.lines[0])


if __name__ == '__main__':
    unittest.main()
