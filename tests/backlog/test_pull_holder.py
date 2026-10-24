"""An out-of-order pull refusal names the in-flight record holding the scope, and narrowing that
record's `scope:` lines with `set` clears the collision, end to end
through Store.write on a no-remote ref store."""
import unittest

from kitlib.backlog.errors import Refusal

from tests.backlog.helpers import TODAY, StoreCase, priced


class PullHolder(StoreCase):
    remote = False

    def test_an_out_of_order_pull_refusal_names_the_in_flight_holder(self):
        self.repo.git('branch', 'tools/holder')
        self.seed(priced('HOLD', state='pulled', pulled_at=TODAY, pulled_by='tools/holder',
                         scope='kitlib/backlog/**'),
                  priced('FOO', scope='kitlib/backlog/scope.py'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('scope kitlib/backlog/scope.py collides with kitlib/backlog/** already taken '
                      'by HOLD (in flight on tools/holder)', str(cm.exception))
        self.assertIn('is a pre-emption, the owner\'s decision', str(cm.exception))
        self.assertIn('Its scope collides with a live pull', str(cm.exception))
        self.verb('set', 'HOLD', [('scope', ['kitlib/backlog/offer.py'])])
        self.verb('pull', 'FOO')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('preempt')), ('pulled', None))

    def test_a_not_offered_refusal_without_a_collision_names_who_may_pre_empt(self):
        self.seed(priced('FOO', scope='kitlib/backlog/scope.py', session='other'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        text = str(cm.exception)
        self.assertIn('is not in `next`\'s offer', text)
        self.assertIn('is a pre-emption, the owner\'s decision: a coordinator asks it through the '
                      'PM and, on the owner\'s yes, runs `pull FOO --reason "<why>"` (recorded); '
                      'a worker stops and reports', text)
        self.assertNotIn('collides', text)


if __name__ == '__main__':
    unittest.main()
