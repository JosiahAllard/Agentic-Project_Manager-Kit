"""`pull` of a pulled record (plan._plan_takeover): the record's own session takes over a dead
pull (no branch by its `pulled_by`) without `release --reason`; another session, a live
`pulled_by` branch, or no session refuses `already in flight` as before."""
import os
from unittest import mock

from kitlib.backlog import offer as omod
from kitlib.backlog.errors import Failure, Refusal, Usage

from tests.backlog.helpers import BRANCH, TODAY, StoreCase, priced

GONE = 'kit/gone-branch'
EARLIER = '2026-10-01'


class PullTakeover(StoreCase):
    remote = False

    def setUp(self):
        super().setUp()
        self.store.session = 'kit'

    def pulled(self, rid, by, session='kit', **over):
        if session is not None:
            over['session'] = session
        return priced(rid, state='pulled', pulled_at=EARLIER, pulled_by=by, **over)

    def test_same_session_takes_over_a_dead_pull(self):
        self.seed(self.pulled('DEAD', GONE, preempt='it was needed first', preempt_by=GONE,
                              preempt_at=EARLIER, displaced='-'))
        change = self.verb('pull', 'DEAD')
        r = self.get('DEAD')
        self.assertEqual((r.get('state'), r.get('pulled_by'), r.get('pulled_at')),
                         ('pulled', BRANCH, TODAY))
        self.assertEqual(r.get('preempt'), 'it was needed first')
        out = '\n'.join(change.lines)
        self.assertIn('took over a dead pull from %s' % GONE, out)
        self.assertIn('taking over a dead pull from %s' % GONE, change.subject)

    def test_a_takeover_takes_no_reason(self):
        self.seed(self.pulled('DEAD', GONE))
        with self.assertRaises(Usage) as cm:
            self.verb('pull', 'DEAD', reason='taking it over from the dead one')
        self.assertIn('--reason', str(cm.exception))
        self.assertEqual(self.get('DEAD').get('pulled_by'), GONE)

    def test_another_session_is_refused(self):
        self.seed(self.pulled('DEAD', GONE, session='other'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'DEAD')
        self.assertEqual(str(cm.exception),
                         '`DEAD` is already in flight - pulled %s by %s' % (EARLIER, GONE))
        self.assertEqual(self.get('DEAD').get('pulled_by'), GONE)

    def test_no_session_is_refused(self):
        self.store.session = None
        self.seed(self.pulled('DEAD', GONE, session=None))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'DEAD')
        self.assertIn('already in flight', str(cm.exception))

    def test_a_takeover_takes_no_ruling_or_blocker(self):
        self.seed(self.pulled('DEAD', GONE))
        for flag, kw in (('--ruling', dict(ruling='R-9001')), ('--blocker', dict(blocker='OTHER'))):
            with self.subTest(flag=flag):
                with self.assertRaises(Usage) as cm:
                    self.verb('pull', 'DEAD', **kw)
                self.assertIn('with no %s' % flag, str(cm.exception))
                self.assertEqual(self.get('DEAD').get('pulled_by'), GONE)

    def test_a_takeover_whose_scope_collides_with_a_live_pull_needs_a_ruling(self):
        self.repo.git('branch', 'kit/live-one')
        self.seed(self.pulled('DEAD', GONE, scope='area/shared/**'),
                  self.pulled('LIVE', 'kit/live-one', session='other', scope='area/shared/x.py'))
        with self.assertRaises(Refusal):
            self.verb('pull', 'DEAD')
        self.assertEqual(self.get('DEAD').get('pulled_by'), GONE)
        change = self.verb('pull', 'DEAD', ruling='R-9001')
        self.assertIn('warning: scope collides with `LIVE`, in flight on kit/live-one',
                      change.lines)
        self.assertEqual(self.get('DEAD').get('pulled_by'), BRANCH)

    def test_a_marked_dead_pull_is_refused_until_the_mark_is_cleared(self):
        self.seed(self.pulled('DEAD', GONE, review='owner-hub 2026-10-15'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'DEAD')
        self.assertIn('marked for review', str(cm.exception))
        self.assertIn('clear-review DEAD', str(cm.exception))
        self.assertEqual(self.get('DEAD').get('pulled_by'), GONE)
        self.verb('clear_review', 'DEAD', 'the coordinator read it')
        self.verb('pull', 'DEAD')
        self.assertEqual(self.get('DEAD').get('pulled_by'), BRANCH)

    def test_a_marked_live_pull_still_refuses_already_in_flight(self):
        self.repo.git('branch', 'kit/live-one')
        self.seed(self.pulled('LIVE', 'kit/live-one', review='owner-hub 2026-10-15'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'LIVE')
        self.assertIn('already in flight', str(cm.exception))

    def test_a_failed_branch_probe_holds_the_pull(self):
        self.seed(self.pulled('DEAD', GONE))
        with mock.patch.object(omod, 'live_branches', return_value=(None, 'git failed')):
            with self.assertRaises(Refusal) as cm:
                self.verb('pull', 'DEAD')
        self.assertIn('already in flight', str(cm.exception))
        self.assertEqual(self.get('DEAD').get('pulled_by'), GONE)

    def land(self, branch, day=EARLIER):
        """A `--no-ff` merge of `branch` on main with the subject a landing writes, committed at
        noon UTC on `day`, then the branch deleted, as a landing and its cleanup leave it."""
        self.repo.git('checkout', '-q', '-b', branch)
        self.repo.write('landed/%s.txt' % branch.replace('/', '-'), 'x\n')
        self.repo.commit('work', 'landed')
        self.repo.git('checkout', '-q', 'main')
        when = '%sT12:00:00+0000' % day
        with mock.patch.dict(os.environ, GIT_COMMITTER_DATE=when, GIT_AUTHOR_DATE=when):
            self.repo.git('merge', '-q', '--no-ff', '-m', 'Merge %s: its summary' % branch,
                          branch)
        self.repo.git('branch', '-D', branch)

    def test_a_landed_pull_is_refused_not_taken_over(self):
        self.land(GONE)          # on the pulled_at day itself: that still counts as landed
        self.seed(self.pulled('DEAD', GONE))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'DEAD')
        self.assertIn('which has landed on main', str(cm.exception))
        self.assertIn('close the record', str(cm.exception))
        self.assertEqual(self.get('DEAD').get('pulled_by'), GONE)

    def test_an_older_landing_of_a_reused_branch_name_does_not_count(self):
        self.land(GONE, day='2026-09-30')
        self.seed(self.pulled('DEAD', GONE))
        change = self.verb('pull', 'DEAD')
        self.assertIn('took over a dead pull from %s' % GONE, '\n'.join(change.lines))
        self.assertEqual(self.get('DEAD').get('pulled_by'), BRANCH)

    def test_a_landed_branch_of_a_longer_name_does_not_count(self):
        self.land(GONE + '-two')
        self.seed(self.pulled('DEAD', GONE))
        self.verb('pull', 'DEAD')
        self.assertEqual(self.get('DEAD').get('pulled_by'), BRANCH)

    def test_a_history_git_cannot_read_fails_closed(self):
        self.ctx.main_branch = 'no-such-main'
        self.seed(self.pulled('DEAD', GONE))
        with self.assertRaises(Failure) as cm:
            self.verb('pull', 'DEAD')
        self.assertIn('cannot read the history of no-such-main', str(cm.exception))
        self.assertEqual(self.get('DEAD').get('pulled_by'), GONE)

    def test_a_live_branch_is_refused(self):
        self.repo.git('branch', 'kit/live-one')
        self.seed(self.pulled('LIVE', 'kit/live-one'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'LIVE')
        self.assertEqual(str(cm.exception),
                         '`LIVE` is already in flight - pulled %s by kit/live-one' % EARLIER)
        self.assertEqual(self.get('LIVE').get('pulled_by'), 'kit/live-one')
