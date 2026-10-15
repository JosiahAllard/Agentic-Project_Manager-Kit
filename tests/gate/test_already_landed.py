"""kitlib.gate.land when the owner commits in the main checkout, and pushes, while the merged gate
runs (KIT-GATE-UNSTICK, then the landing tree; docs/gate.md "Landing", steps 5 to 9).

The merge and its gate are in the kit's landing tree, never in the main checkout, so nothing the
owner commits there is ever on top of the gated merge. A commit the owner pushes moves the remote's
main, the push of the merge is refused (a landing never forces), and the landing ends NOT LANDED
(FAILED): nothing reset, the lock released, the owner's commits, edits and files untouched, the
branch kept to land again. No landing finishes on commits no gate saw: LANDED_UNGATED (exit 9) is
no longer produced. A STUCK report (a push git cannot settle) never says reset.

The merged gate is injected: it plays the owner, committing in the main checkout and pushing.
Scratch repositories with a bare remote in temp and the scratch lock store only."""
import os
import sys
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import result
from kitlib.git import refs
from tests.gate import fixtures
from tests.gate.test_land import BRANCH
from tests.gate.test_land_store import StoreLandCase, pulled
from tests.gate.test_stuck import _Stuck, commands, joined
from tests.realgit import real_git

GREEN = G.Outcome(G.OK, ['GATE OK'], verdict='GATE OK', results=[])
RED = G.Outcome(G.FAILED, ['GATE FAILED: unit (unit: exit 1)'], verdict='GATE FAILED: unit',
                results=[])
# `unit` runs only for python files, so a commit on top that changes a doc selects no step.
GLOBBED = {'steps': [{'name': 'unit', 'globs': ['*.py'], 'cmd': fixtures.RED_AWARE,
                      'pass': '^ok$'}]}


def from_callers(name, callers, value, times=None):
    """Patches refs.<name> to answer `value` when called straight from a Lander method named in
    `callers` (`times` times, or every time when None); every other caller gets git's answer."""
    real = getattr(refs, name)
    left = [times]

    def call(*a, **kw):
        if sys._getframe(1).f_code.co_name in callers and left[0] != 0:
            if left[0] is not None:
                left[0] -= 1
            return value
        return real(*a, **kw)
    return mock.patch.object(refs, name, call)


class _OnTop(_Stuck):
    """Landings whose lock holder is a dead process (so a printed break command can break it)."""
    GATE = GLOBBED

    def owner(self, files, push=True, after=None, verdict=GREEN):
        """A merged gate that commits `files` in the main checkout (the owner), pushes main when
        `push`, then runs `after()`, and returns `verdict`. Its commit goes to self.extra."""
        def gate(lander):
            for rel, text in files.items():
                self.repo.write(rel, text)
            self.extra = self.repo.commit('owner edit', *files)
            if push:
                self.repo.git('push', '-q', 'origin', 'main')
            if after is not None:
                after()
            return verdict
        return gate

    def land(self, gate, branches=None):
        if branches is None:
            _, self.tip = self.branch('feature', {'a.py': 'x = 1\n'})
            return self.lander(BRANCH, self.main, self.tip, merged_gate=gate).run()
        tips = [self.branch(b, {'%s.py' % b: 'x = 1\n'})[1] for b in branches]
        return self.lander(['kit/%s' % b for b in branches], self.main, tips,
                           merged_gate=gate).run()

    def assert_refused(self, out):
        """NOT LANDED because the owner's push moved the remote's main: the refusal names that
        commit, nothing says reset, the remote holds the owner's commit and not the merge, and the
        lock is released. Never LANDED, never exit 9, never STUCK."""
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertNotEqual(out.code, G.LANDED_UNGATED)
        text = joined(out)
        self.assertIn('origin/main moved on to %s while the merged gate ran' % self.extra[:10],
                      text)
        self.assertIn('a landing never forces', text)
        self.assertTrue(out.lines[-1].startswith('NOT LANDED: '), out.lines)
        self.assertFalse([l for l in out.lines if l.startswith(('LANDED', 'STUCK', 'WARNING'))],
                         out.lines)
        self.assertNotIn('reset', text.lower(), out.lines)
        self.assertEqual(self.remote_main(), self.extra)
        self.assertEqual(self.repo.git('rev-parse', 'refs/remotes/origin/main'), self.extra)
        self.assertTrue(out.merge)
        self.assertFalse(self.ancestor(out.merge, self.extra), out.lines)
        self.assertIsNone(self.lock().read())

    def ancestor(self, a, b):
        return self.repo.git('merge-base', a, b) == a

    def assert_no_reset(self, out):
        """A STUCK report: nothing in it says reset, since a reset could put main behind a remote
        that already holds the merge (the hub-validate landing printed `reset --keep <base>`). The
        lock is left at phase stuck."""
        self.assertEqual(out.code, G.STUCK, out.lines)
        self.assertNotIn('reset', joined(out).lower(), out.lines)
        self.assertEqual(self.lock().read()['phase'], 'stuck')

    def run_printed(self, out):
        """Runs every printed command; the last is the break command, and it breaks the lock."""
        printed = None
        for c in commands(out):
            printed = self.sh(c)
        self.assert_broken(printed)


class OwnerPushes(_OnTop):
    def test_a_doc_commit_pushed_mid_gate_is_not_landed(self):             # T1
        out = self.land(self.owner({'docs/note.md': 'owner\n'}))
        self.assert_refused(out)
        self.assertEqual(self.main_now(), self.extra)                  # the owner's commit kept
        self.assertEqual(self.head_branch(), 'main')
        self.assertEqual(self.repo.git('status', '--porcelain'), '')
        got, why = result.read(self.repo.root, BRANCH, 'land')
        self.assertIsNone(why)
        self.assertEqual((got['verdict'], got['code']), ('fail', G.FAILED))
        self.assertEqual(self.repo.git('rev-parse', BRANCH), self.tip)  # kept, to land again
        self.assertTrue(os.path.isdir(self.repo.path('.claude', 'worktrees', 'feature')))

    def test_a_gated_path_pushed_mid_gate_is_not_landed_and_never_exit_9(self):  # T2
        out = self.land(self.owner({'tools/x.py': 'y = 2\n'}))
        self.assert_refused(out)
        self.assertNotIn('not gated whole', joined(out))
        got, why = result.read(self.repo.root, BRANCH, 'land')
        self.assertIsNone(why)
        self.assertEqual((got['verdict'], got['code']), ('fail', G.FAILED))

    def test_a_batch_with_a_commit_pushed_mid_gate_lands_no_branch(self):   # T7
        out = self.land(self.owner({'docs/note.md': 'owner\n'}), branches=['one', 'two'])
        self.assert_refused(out)
        for b in ('kit/one', 'kit/two'):
            got, why = result.read(self.repo.root, b, 'land')
            self.assertIsNone(why)
            self.assertEqual(got['verdict'], 'fail', b)

    def test_landing_again_after_the_refusal_lands_on_the_owners_commit(self):
        out = self.land(self.owner({'docs/note.md': 'owner\n'}))
        self.assert_refused(out)
        again = self.lander(BRANCH, self.extra, self.tip).run()
        self.assertEqual(again.code, G.OK, again.lines)
        self.assertEqual(self.remote_main(), again.merge)
        self.assertEqual(self.repo.git('rev-parse', again.merge + '^1'), self.extra)
        self.assertEqual(self.main_now(), again.merge)
        self.assertIsNone(self.lock().read())


class OwnerWorkKept(_OnTop):
    def test_a_commit_not_yet_pushed_is_kept_and_not_reset(self):          # T3
        def local():
            self.repo.write('docs/local.md', 'local\n')
            self.local = self.repo.commit('owner local', 'docs/local.md')
        out = self.land(self.owner({'docs/note.md': 'owner\n'}, after=local))
        self.assert_refused(out)
        self.assertEqual(self.main_now(), self.local)                  # nothing moved
        self.assertEqual(self.remote_main(), self.extra)

    def test_an_untracked_file_is_kept_and_not_reset(self):                # T4
        def litter():
            self.repo.write('scratch.txt', 'left\n')
        out = self.land(self.owner({'docs/note.md': 'owner\n'}, after=litter))
        self.assert_refused(out)
        self.assertEqual(self.main_now(), self.extra)
        with open(self.repo.path('scratch.txt'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'left\n')

    def test_an_untracked_file_in_the_main_checkout_does_not_stop_the_landing(self):
        # The owner pushes nothing new (main is still the base there) and leaves a file: the merge
        # is not in the main checkout, so the landing pushes it, and the main checkout is
        # fast-forwarded to it with the file carried. At base this was STUCK, and once printed
        # a reset behind the remote.
        def gate(lander):
            self.repo.git('push', '-q', 'origin', 'main')
            self.repo.write('scratch.txt', 'left\n')
            return GREEN
        out = self.land(gate)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse([l for l in out.lines if l.startswith(('STUCK', 'WARNING'))], out.lines)
        self.assertEqual(self.remote_main(), out.merge)
        self.assertEqual(self.main_now(), out.merge)
        self.assertEqual(self.repo.git('status', '--porcelain'), '?? scratch.txt')
        self.assertIsNone(self.lock().read())

    def test_a_red_gate_while_the_owner_pushes_is_not_landed_and_not_reset(self):  # T6
        out = self.land(self.owner({'docs/note.md': 'owner\n'}, verdict=RED))
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertTrue(out.lines[-1].startswith('NOT LANDED: '), out.lines)
        self.assertNotIn('without a passing', joined(out))
        self.assertNotIn('reset', joined(out).lower())
        self.assertEqual(self.records(), [])
        self.assertEqual(self.main_now(), self.extra)
        self.assertEqual(self.remote_main(), self.extra)
        self.assertIsNone(self.lock().read())

    def test_an_uncommitted_edit_is_kept_and_not_reset(self):
        # The hub-validate shape: green, the owner's commit pushed, and an uncommitted edit in
        # the main checkout. At base the report went through clear_steps and printed
        # `reset --keep <base>`.
        def edit():
            self.repo.write('README.md', 'edited, not committed\n')
        out = self.land(self.owner({'docs/note.md': 'owner\n'}, after=edit))
        self.assert_refused(out)
        self.assertEqual(self.main_now(), self.extra)
        with open(self.repo.path('README.md'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'edited, not committed\n')


@real_git('the push of main refused as a non-fast-forward while HEAD is on another branch: the '
          'fake raises Unsupported for git\'s hint')
class HeadElsewhere(_OnTop):
    def test_head_switched_after_the_push_is_kept_and_not_reset(self):     # T5
        def switch():
            self.repo.git('switch', '-q', '-c', 'elsewhere')
        out = self.land(self.owner({'docs/note.md': 'owner\n'}, after=switch))
        self.assert_refused(out)
        self.assertEqual(self.head_branch(), 'elsewhere')
        self.assertEqual(self.main_now(), self.extra)


class PushUnsettled(_OnTop):
    """The owner's push and a remote git cannot read or place: whether the merge landed cannot be
    told, so STUCK, and still nothing says reset."""

    def test_ls_remote_failing_after_the_push_is_stuck_and_not_reset(self):
        with from_callers('ls_remote', {'push'}, (None, 'network down')):
            out = self.land(self.owner({'docs/note.md': 'owner\n'}))
        self.assert_no_reset(out)
        self.assertIn('unreadable (network down)', joined(out))
        self.assertEqual(self.main_now(), self.extra)
        self.run_printed(out)
        self.assertEqual(self.main_now(), self.extra)
        self.assertEqual(self.remote_main(), self.extra)

    def test_ls_remote_failing_throughout_is_stuck_and_not_reset(self):
        with from_callers('ls_remote', {'push', 'leftovers'}, (None, 'network down')):
            out = self.land(self.owner({'docs/note.md': 'owner\n'}))
        self.assert_no_reset(out)
        self.assertEqual(self.main_now(), self.extra)
        self.run_printed(out)
        self.assertEqual(self.main_now(), self.extra)
        self.assertEqual(self.remote_main(), self.extra)

    def test_a_remote_main_git_cannot_place_prints_no_reset(self):
        # Whether the remote's main holds the merge cannot be told: the report says how to find
        # out, and prints no reset (nor a branch to keep commits for one).
        def litter():
            self.repo.write('scratch.txt', 'left\n')
        with from_callers('is_ancestor', {'on_remote'}, (None, 'not fetched')):
            out = self.land(self.owner({'docs/note.md': 'owner\n'}, after=litter))
        self.assert_no_reset(out)
        self.assertNotIn('kit-stuck', joined(out))
        self.assertIn('cannot be told', joined(out))
        self.assertTrue(any(' branch -r --contains %s' % out.merge in c for c in commands(out)),
                        out.lines)
        self.run_printed(out)
        self.assertEqual(self.main_now(), self.extra)


class ClosesRecords(StoreLandCase):
    GATE = GLOBBED

    def test_the_pulled_record_stays_open_when_the_push_is_refused(self):  # T8
        self.seed(pulled('FOO'))
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})

        def gate(lander):
            self.repo.write('docs/note.md', 'owner\n')
            self.repo.commit('owner edit', 'docs/note.md')
            self.repo.git('push', '-q', 'origin', 'main')
            return GREEN
        out = self.lander(BRANCH, self.main, tip, merged_gate=gate).run()
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertEqual(self.stems(), ['FOO'])
        self.assertFalse([s for s in self.subjects() if 'closes FOO' in s])
        self.assertIsNone(self.lock().read())


if __name__ == '__main__':
    unittest.main()
