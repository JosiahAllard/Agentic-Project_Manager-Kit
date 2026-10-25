"""Blocking or releasing a pulled record whose work stopped (kitlib.backlog.plan stop_pull,
plan_block, plan_release; docs/backlog.md "Stopping a pull"): its holder may, and anybody may when
the pull is dead or `Context.liveness` finds its worktree dead. One commit frees the scope, the
body says where the work was left, and the branch is never touched."""
import os
import time
import unittest
from unittest import mock

from kitlib.backlog import check
from kitlib.backlog import offer as omod
from kitlib.backlog import opener
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Refusal, Usage
from kitlib.worktree import classify
from tests.backlog.helpers import BRANCH, TODAY, StoreCase, priced

OTHER = 'tools/other-branch'
WHY = 'its worktree went quiet days ago'
PULLED = dict(state='pulled', pulled_at=TODAY, preempt='2', preempt_by='R-9001',
              preempt_at=TODAY, displaced='BAR')


class StoppedCase(StoreCase):
    remote = False

    def commits(self):
        return int(self.repo.git('rev-list', '--count', smod.STORE_REF))

    def tip(self, branch):
        return self.repo.git('rev-parse', 'refs/heads/%s' % branch)

    def assert_freed(self, rid):
        r = self.get(rid)
        for key in ('pulled_by', 'pulled_at', 'preempt', 'preempt_by', 'preempt_at', 'displaced'):
            self.assertIsNone(r.get(key), key)
        holding, dead, why = omod.in_flight_split(self.store.load(), self.repo.root, 'origin')
        self.assertEqual((holding, dead, why), ([], [], None))


class Block(StoppedCase):

    def test_the_holder_blocks_its_own_pull_in_one_commit_keeping_the_branch(self):
        self.seed(priced('FOO', pulled_by=BRANCH, **PULLED))
        before, tip = self.commits(), self.tip(BRANCH)
        change = self.verb('block', 'FOO', ['live-client'], reason=WHY)
        self.assertEqual(self.commits(), before + 1)
        self.assertEqual(self.tip(BRANCH), tip)
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('blocked_on')), ('blocked', ['live-client']))
        self.assert_freed('FOO')
        self.assertIn('Stopped on branch %s at %s, branch kept.\nBlocked on live-client (%s): %s\n'
                      % (BRANCH, tip[:10], TODAY, WHY), r.body)
        self.assertIn('  was pulled_by: %s' % BRANCH, change.lines)

    def test_a_ruling_cited_in_the_reason_of_a_stopped_pull_keeps_the_hold(self):
        # The Stopped line is its own body line, so the `Blocked on` line still starts one and
        # record.hold_citations reads the ruling in it: check finds no void hold.
        self.seed(priced('FOO', pulled_by=BRANCH, **PULLED))
        self.verb('block', 'FOO', ['deliberate'], reason='R-9234: until X')
        snap = self.store.load()
        self.assertEqual(snap.get('FOO').get('state'), 'blocked')
        self.assertIn('Stopped on branch %s' % BRANCH, snap.get('FOO').body)
        self.assertEqual(check.check_hold_citations(snap).warnings, [])

    def test_anybody_blocks_a_pull_whose_worktree_the_probe_finds_dead(self):
        self.repo.git('branch', OTHER)
        self.seed(priced('FOO', pulled_by=OTHER, **PULLED))
        asked = []
        self.ctx.liveness = lambda b: (asked.append(b), (None, self.tip(OTHER)))[1]
        change = self.verb('block', 'FOO', ['live-client'], reason=WHY)
        self.assertEqual(asked, [OTHER])
        self.assert_freed('FOO')
        self.assertIn('Stopped on branch %s at %s, branch kept.' % (OTHER, self.tip(OTHER)[:10]),
                      self.get('FOO').body)
        self.assertIn('  was pulled_by: %s (its worktree is dead)' % OTHER, change.lines)

    def test_a_live_tree_refuses_and_names_why(self):
        self.repo.git('branch', OTHER)
        self.seed(priced('FOO', pulled_by=OTHER, **PULLED))
        self.ctx.liveness = lambda b: ('active 1.0 h ago', None)
        with self.assertRaises(Refusal) as cm:
            self.verb('block', 'FOO', ['live-client'], reason=WHY)
        self.assertEqual(str(cm.exception),
                         '`FOO` is in flight (pulled by %s), and its tree is live: active 1.0 h '
                         'ago. Its holder blocks or releases it: from that branch, or as that '
                         'branch from any checkout (backlog.py -C <store> --as %s block FOO --on '
                         '<member> --reason "<why>")' % (OTHER, OTHER))
        self.assertEqual(self.get('FOO').get('state'), 'pulled')

    def test_the_refusal_names_the_records_session_in_the_as_form(self):
        # A coordinator that pulled with `--as` is the holder; a worker on that branch is refused
        # by the guard (own pulls only), so the refusal gives the coordinator its working form.
        self.repo.git('branch', OTHER)
        self.seed(priced('FOO', pulled_by=OTHER, session='kit', **PULLED))
        self.ctx.liveness = lambda b: ('active 1.0 h ago', None)
        with self.assertRaises(Refusal) as cm:
            self.verb('block', 'FOO', ['live-client'], reason=WHY)
        self.assertIn('(backlog.py -C <store> --session kit --as %s block FOO --on <member> '
                      '--reason "<why>")' % OTHER, str(cm.exception))

    def test_no_probe_refuses_a_live_branch(self):
        self.repo.git('branch', OTHER)
        self.seed(priced('FOO', pulled_by=OTHER, **PULLED))
        self.assertIsNone(self.ctx.liveness)
        with self.assertRaises(Refusal) as cm:
            self.verb('block', 'FOO', ['live-client'], reason=WHY)
        self.assertIn('a branch or worktree by that name exists', str(cm.exception))

    def test_a_dead_pull_blocks_with_no_stopped_line(self):
        self.seed(priced('FOO', pulled_by='tools/gone', **PULLED))
        change = self.verb('block', 'FOO', ['live-client'], reason=WHY)
        self.assert_freed('FOO')
        self.assertNotIn('Stopped on branch', self.get('FOO').body)
        self.assertIn('  was pulled_by: tools/gone (dead)', change.lines)

    def test_blocking_a_pulled_record_needs_a_reason(self):
        self.seed(priced('FOO', pulled_by=BRANCH, **PULLED))
        with self.assertRaises(Usage) as cm:
            self.verb('block', 'FOO', ['live-client'])
        self.assertEqual(str(cm.exception), 'blocking a pulled record frees its scope: it needs '
                                            '`--reason T`, written to the body')

    def test_the_real_probe_lets_a_dead_worktree_be_blocked(self):
        # opener's seam end to end: classify.branch_liveness on this repository.
        path = self.repo.worktree('other', OTHER)
        self.repo.write('w.txt', 'work\n', root=path)
        self.repo.commit('work', 'w.txt', cwd=path)
        self.seed(priced('FOO', pulled_by=OTHER, **PULLED))
        self.ctx.liveness = lambda b: classify.branch_liveness(
            self.repo.root, b, now=time.time() + 3 * 24 * 3600, machine=lambda: ([], None),
            procs=lambda: (([], 0, 1), None))
        self.verb('block', 'FOO', ['live-client'], reason=WHY)
        self.assert_freed('FOO')
        self.assertTrue(os.path.isdir(path))
        self.ctx.liveness = lambda b: classify.branch_liveness(
            self.repo.root, b, now=time.time(), machine=lambda: ([], None),
            procs=lambda: (([], 0, 1), None))
        self.seed(priced('BAR', pulled_by=OTHER, **PULLED))
        with self.assertRaises(Refusal) as cm:
            self.verb('block', 'BAR', ['live-client'], reason=WHY)
        self.assertIn('its tree is live: active', str(cm.exception))


class Release(StoppedCase):

    def test_anybody_releases_a_pull_whose_worktree_the_probe_finds_dead(self):
        self.repo.git('branch', OTHER)
        self.seed(priced('FOO', pulled_by=OTHER, **PULLED))
        tip = self.tip(OTHER)
        self.ctx.liveness = lambda b: (None, tip)
        before = self.commits()
        change = self.verb('release', 'FOO', WHY)
        self.assertEqual(self.commits(), before + 1)
        self.assertEqual(self.tip(OTHER), tip)
        self.assertEqual(self.get('FOO').get('state'), 'open')
        self.assert_freed('FOO')
        self.assertIn('Stopped on branch %s at %s, branch kept.\nReleased (%s): %s'
                      % (OTHER, tip[:10], TODAY, WHY), self.get('FOO').body)
        self.assertIn('  branch kept: %s at %s' % (OTHER, tip[:10]), change.lines)

    def test_a_live_tree_refuses_the_release_and_names_why(self):
        self.repo.git('branch', OTHER)
        self.seed(priced('FOO', pulled_by=OTHER, **PULLED))
        self.ctx.liveness = lambda b: ('machine lock: r/x run 7', None)
        with self.assertRaises(Refusal) as cm:
            self.verb('release', 'FOO', WHY)
        self.assertEqual(str(cm.exception),
                         'the pull of `FOO` by `%s` is live (a branch or worktree by that name '
                         'exists); its tree is live: machine lock: r/x run 7. Its holder releases '
                         'it: from that branch, or as that branch from any checkout (backlog.py '
                         '-C <store> --as %s release FOO --reason "<why>")' % (OTHER, OTHER))

    def test_a_dead_pull_releases_with_no_stopped_line(self):
        self.seed(priced('FOO', pulled_by='tools/gone', **PULLED))
        self.verb('release', 'FOO', WHY)
        self.assertNotIn('Stopped on branch', self.get('FOO').body or '')


class PullResume(StoppedCase):
    """`pull` of a record whose body carries stop_pull's `Stopped on branch` note names that
    branch and tip (plan.resume_line), so the puller resumes from it."""

    def note(self, branch, tip):
        return 'Stopped on branch %s at %s, branch kept.' % (branch, tip)

    def resume(self, change):
        return [line for line in change.lines if 'resume on' in line]

    def test_a_pull_of_a_record_with_the_note_names_the_branch_and_tip(self):
        self.seed(priced('FOO', body='Some text.\n\n%s\nReleased (%s): %s'
                         % (self.note(OTHER, 'abcdef1234'), TODAY, WHY)))
        change = self.verb('pull', 'FOO')
        self.assertEqual(self.resume(change),
                         ['  resume on %s? Work stopped there at abcdef1234, branch kept' % OTHER])

    def test_no_note_prints_nothing(self):
        self.seed(priced('FOO', body='Some text, no stopped branch.'))
        self.assertEqual(self.resume(self.verb('pull', 'FOO')), [])

    def test_of_two_notes_the_latest_wins(self):
        self.seed(priced('FOO', body='%s\nReleased (%s): first\n\n%s\nReleased (%s): second'
                         % (self.note('kit/first', '1111111111'), TODAY,
                            self.note('kit/second', '2222222222'), TODAY)))
        self.assertEqual(self.resume(self.verb('pull', 'FOO')),
                         ['  resume on kit/second? Work stopped there at 2222222222, branch kept'])

    def test_a_takeover_of_a_dead_pull_names_the_branch_too(self):
        self.store.session = 'kit'
        self.seed(priced('FOO', state='pulled', pulled_at=TODAY, pulled_by='kit/gone-branch',
                         session='kit', body=self.note(OTHER, 'abcdef1234')))
        change = self.verb('pull', 'FOO')
        self.assertIn('took over a dead pull from kit/gone-branch', '\n'.join(change.lines))
        self.assertEqual(self.resume(change),
                         ['  resume on %s? Work stopped there at abcdef1234, branch kept' % OTHER])

    def test_the_note_block_writes_is_the_note_pull_reads(self):
        # stop_pull's own text, not a copy of it: block, unblock, then pull. The branch is gone
        # by the unblock, so the record opens (a live one gets its pull back:
        # tests/backlog/test_unblock_restores_pull.py).
        self.repo.git('branch', OTHER)
        self.seed(priced('FOO', pulled_by=OTHER, **PULLED))
        tip = self.tip(OTHER)
        self.ctx.liveness = lambda b: (None, tip)
        self.verb('block', 'FOO', ['live-client'], reason=WHY)
        self.repo.git('branch', '-D', OTHER)
        self.verb('unblock', 'FOO')
        self.assertEqual(self.resume(self.verb('pull', 'FOO')),
                         ['  resume on %s? Work stopped there at %s, branch kept'
                          % (OTHER, tip[:10])])


class Opener(unittest.TestCase):

    def test_the_probe_is_the_classifiers_imported_when_asked(self):
        with mock.patch.object(classify, 'branch_liveness',
                               return_value=(None, 'abc')) as probe:
            self.assertEqual(opener.liveness('Q:/repo', 'kit/x'), (None, 'abc'))
        probe.assert_called_once_with('Q:/repo', 'kit/x')


if __name__ == '__main__':
    unittest.main()
