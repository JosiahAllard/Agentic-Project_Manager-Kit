"""`pull` of a pulled record (plan._plan_takeover): the record's own session takes over a dead
pull (no branch by its `pulled_by`) without `release --reason`; another session, a live
`pulled_by` branch, or no session refuses `already in flight` as before."""
from kitlib.backlog.errors import Refusal, Usage

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

    def test_a_live_branch_is_refused(self):
        self.repo.git('branch', 'kit/live-one')
        self.seed(self.pulled('LIVE', 'kit/live-one'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'LIVE')
        self.assertEqual(str(cm.exception),
                         '`LIVE` is already in flight - pulled %s by kit/live-one' % EARLIER)
        self.assertEqual(self.get('LIVE').get('pulled_by'), 'kit/live-one')
