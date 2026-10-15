"""A pull whose scope collides with a live pull needs `--ruling R-nnnn`; `--reason` alone is not
enough. `set --scope` on a pulled record runs the same check when it
widens the scope, and narrowing stays free, so `set` is not a second door around the pull check.
End to end through Store.write on a no-remote ref store."""
import unittest

from kitlib.backlog import plan as pmod
from kitlib.backlog.errors import Refusal, Usage

from tests.backlog.helpers import BRANCH, TODAY, StoreCase, parse, priced, rec

HOLDER = 'kit/holder'


def hold(scope='kitlib/backlog/**', rid='HOLD', by=HOLDER):
    return priced(rid, state='pulled', pulled_at=TODAY, pulled_by=by, scope=scope)


def mine(scope, rid='MINE'):
    return priced(rid, state='pulled', pulled_at=TODAY, pulled_by=BRANCH, scope=scope)


class PullCollision(StoreCase):
    remote = False

    def setUp(self):
        super().setUp()
        self.repo.git('branch', HOLDER)

    def test_a_colliding_pull_with_a_reason_alone_is_refused(self):
        self.seed(hold(), priced('FOO', scope='kitlib/backlog/scope.py'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO', reason='the owner wants it today')
        msg = str(cm.exception)
        self.assertIn('kitlib/backlog/scope.py collides with kitlib/backlog/**', msg)
        self.assertIn('HOLD (in flight on %s)' % HOLDER, msg)
        self.assertIn('--ruling R-nnnn', msg)
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_a_colliding_pull_with_no_reason_names_the_ruling_too(self):
        self.seed(hold(), priced('FOO', scope='kitlib/backlog/scope.py'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('already taken by HOLD (in flight on %s)' % HOLDER, str(cm.exception))
        self.assertIn('--ruling R-nnnn', str(cm.exception))

    def test_a_colliding_pull_with_a_ruling_is_written_and_cites_it(self):
        self.seed(hold(), priced('FOO', scope='kitlib/backlog/scope.py'))
        change = self.verb('pull', 'FOO', reason='the owner wants it today', ruling='R-9001')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('preempt')),
                         ('pulled', 'the owner wants it today (PM R-9001)'))
        self.assertEqual(change.ruling, 'R-9001')

    def test_a_colliding_pull_with_a_bad_ruling_shape_is_usage(self):
        self.seed(hold(), priced('FOO', scope='kitlib/backlog/scope.py'))
        with self.assertRaises(Usage):
            self.verb('pull', 'FOO', reason='the owner wants it today', ruling='9001')
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_a_collision_with_a_dead_pull_needs_no_ruling(self):
        self.seed(hold(by='kit/gone'), priced('FOO', scope='kitlib/backlog/scope.py'))
        self.verb('pull', 'FOO')
        self.assertEqual(self.get('FOO').get('state'), 'pulled')

    def test_an_out_of_order_pull_with_no_collision_still_takes_a_reason_alone(self):
        self.seed(hold(), priced('TOP', priority='1', scope='docs/**'),
                  priced('FOO', priority='5', scope='docs/x.md'))
        self.verb('pull', 'FOO', reason='needed before the others')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('preempt')), ('pulled', 'needed before the others'))

    def test_one_ruling_covers_a_minus_one_record_that_also_collides(self):
        self.seed(hold(), rec('MIN', priority='-1', owner_ask='which?',
                              scope='kitlib/backlog/scope.py'))
        with self.assertRaises(Refusal):
            self.verb('pull', 'MIN', reason='the owner said go')
        change = self.verb('pull', 'MIN', reason='the owner said go', ruling='R-9001')
        r = self.get('MIN')
        self.assertEqual((r.get('state'), r.get('hold_ruling'), r.get('preempt')),
                         ('pulled', 'R-9001', 'the owner said go (PM R-9001)'))
        self.assertEqual(change.ruling, 'R-9001')

    def test_a_pull_that_overlaps_a_record_on_the_callers_own_branch_pulls_plainly(self):
        # Bundling overlapping records onto one branch is not a
        # collision, in the offer either: the branch already holds that scope, so its own pulls
        # are left out of the offer `pull` checks against. It once took `--reason` ("passed over
        # ... already taken by MINE (in flight on <the caller's branch>)").
        self.seed(mine('kitlib/backlog/**'), priced('FOO', scope='kitlib/backlog/scope.py'))
        change = self.verb('pull', 'FOO')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('pulled_by'), r.get('preempt')),
                         ('pulled', BRANCH, None))
        self.assertIsNone(change.ruling)
        self.assertEqual(change.subject, 'backlog: pull FOO onto %s' % BRANCH)

    def test_a_second_bundled_pull_onto_the_same_branch_pulls_plainly_too(self):
        self.seed(mine('kitlib/backlog/**'), priced('FOO', scope='kitlib/backlog/record.py'),
                  priced('BAR', scope='kitlib/backlog/plan.py'))
        self.verb('pull', 'FOO')
        self.verb('pull', 'BAR')
        self.assertEqual(self.get('BAR').get('pulled_by'), BRANCH)
        self.assertIsNone(self.get('BAR').get('preempt'))

    def test_own_branch_overlap_does_not_excuse_a_collision_with_another_branch(self):
        self.seed(mine('kitlib/backlog/**'), hold(scope='kitlib/backlog/scope.py'),
                  priced('FOO', scope='kitlib/backlog/scope.py'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('HOLD (in flight on %s)' % HOLDER, str(cm.exception))
        self.assertIn('--ruling R-nnnn', str(cm.exception))
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_the_offer_still_ranks_records_the_branch_does_not_hold(self):
        # Only the caller's own pulls are left out: a higher-ranked open record still comes first.
        self.seed(mine('kitlib/backlog/plan.py'), priced('TOP', priority='1', scope='docs/**'),
                  priced('FOO', priority='5', scope='docs/x.md'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('TOP (offered above)', str(cm.exception))


class SetScopeWidening(StoreCase):
    remote = False

    def setUp(self):
        super().setUp()
        self.repo.git('branch', HOLDER)

    def test_widening_into_a_live_pull_is_refused(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/plan.py'))
        with self.assertRaises(Refusal) as cm:
            self.verb('set', 'MINE', [('scope', ['kitlib/backlog/plan.py', 'kitlib/backlog/**'])])
        msg = str(cm.exception)
        self.assertIn('kitlib/backlog/** collides with kitlib/backlog/offer.py', msg)
        self.assertIn('HOLD (in flight on %s)' % HOLDER, msg)
        self.assertIn('--ruling R-nnnn', msg)
        self.assertEqual(self.get('MINE').all('scope'), ['kitlib/backlog/plan.py'])

    def test_widening_into_a_live_pull_with_a_ruling_is_written(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/plan.py'))
        change = self.verb('set', 'MINE', [('scope', ['kitlib/backlog/**'])], ruling='R-9001')
        self.assertEqual(self.get('MINE').all('scope'), ['kitlib/backlog/**'])
        self.assertEqual(change.ruling, 'R-9001')

    def test_widening_into_free_space_is_free(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/plan.py'))
        self.verb('set', 'MINE', [('scope', ['kitlib/backlog/plan.py', 'docs/backlog.md'])])
        self.assertEqual(self.get('MINE').all('scope'), ['kitlib/backlog/plan.py', 'docs/backlog.md'])

    def test_a_ruling_on_a_widening_that_collides_with_nothing_is_usage(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/plan.py'))
        with self.assertRaises(Usage):
            self.verb('set', 'MINE', [('scope', ['docs/backlog.md'])], ruling='R-9001')

    def test_narrowing_stays_free_even_when_the_old_scope_collides(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/**'))
        self.verb('set', 'MINE', [('scope', ['kitlib/backlog/offer.py'])])
        self.assertEqual(self.get('MINE').all('scope'), ['kitlib/backlog/offer.py'])

    def test_an_open_record_widens_freely(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), priced('FOO', scope='docs/x.md'))
        self.verb('set', 'FOO', [('scope', ['kitlib/backlog/**'])])
        self.assertEqual(self.get('FOO').all('scope'), ['kitlib/backlog/**'])

    def test_a_widening_with_a_ruling_and_a_price_change_is_usage(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/plan.py'))
        with self.assertRaises(Usage) as cm:
            self.verb('set', 'MINE', [('scope', ['kitlib/backlog/**']), ('priority', ['1']),
                                      ('priority_by', ['pm'])], ruling='R-9001')
        self.assertIn('one ruling, one change', str(cm.exception).lower())
        self.assertIn('a price move', str(cm.exception))

    def test_a_widening_with_a_ruling_and_an_owner_hold_release_is_usage(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'),
                  priced('MINE', state='pulled', pulled_at=TODAY, pulled_by=BRANCH,
                         scope='kitlib/backlog/plan.py', blocked_on='owner'))
        with self.assertRaises(Usage) as cm:
            self.verb('set', 'MINE', [('scope', ['kitlib/backlog/**']), ('blocked_on', ['-'])],
                      ruling='R-9001')
        self.assertIn('one ruling, one change', str(cm.exception).lower())
        self.assertIn('an owner-hold release', str(cm.exception))
        self.assertEqual(self.get('MINE').all('scope'), ['kitlib/backlog/plan.py'])

    def test_widening_never_collides_with_the_record_itself(self):
        # MINE is a live pull too; its own old scope is under the new one.
        self.seed(mine('kitlib/backlog/plan.py'))
        self.verb('set', 'MINE', [('scope', ['kitlib/backlog/**'])])
        self.assertEqual(self.get('MINE').all('scope'), ['kitlib/backlog/**'])

    def test_widening_into_a_sibling_on_the_same_branch_is_free(self):
        self.seed(mine('kitlib/backlog/offer.py', rid='SIB'), mine('kitlib/backlog/plan.py'))
        change = self.verb('set', 'MINE', [('scope', ['kitlib/backlog/**'])])
        self.assertEqual(self.get('MINE').all('scope'), ['kitlib/backlog/**'])
        self.assertIsNone(change.ruling)

    def test_the_branch_that_counts_is_the_records_not_the_callers(self):
        # A coordinator on another branch widens MINE into SIB: both are pulled by BRANCH, so it
        # is free. Widening OTHER (pulled by HOLDER) into SIB still collides.
        self.seed(mine('kitlib/backlog/offer.py', rid='SIB'), mine('kitlib/backlog/plan.py'),
                  hold(scope='docs/x.md', rid='OTHER'))
        self.ctx.branch = 'kit/coordinator'
        self.repo.git('branch', 'kit/coordinator')
        self.verb('set', 'MINE', [('scope', ['kitlib/backlog/**'])])
        with self.assertRaises(Refusal) as cm:
            self.verb('set', 'OTHER', [('scope', ['docs/x.md', 'kitlib/backlog/offer.py'])])
        self.assertIn('SIB (in flight on %s)' % BRANCH, str(cm.exception))


class LiveHits(unittest.TestCase):
    """pmod.live_hits directly: the arms an end-to-end store cannot reach."""

    def pulled(self, rid, scope, by=HOLDER):
        return parse(stem=rid, id=rid, scope=scope, state='pulled', pulled_at=TODAY,
                     pulled_by=by, priority='2', priority_by='agent')

    def test_a_scope_that_cannot_be_compared_is_a_hit(self):
        hits = pmod.live_hits(['kitlib/backlog/plan.py'], [self.pulled('BAD', 'kitlib/*.py')])
        self.assertEqual(len(hits), 1, hits)
        self.assertIn('cannot be compared with BAD (in flight on %s)' % HOLDER, hits[0])

    def test_skip_drops_the_record_itself(self):
        holding = [self.pulled('SELF', 'a/x.py'), self.pulled('OTHER', 'b/y.py')]
        self.assertEqual(pmod.live_hits(['a/**'], holding, skip='SELF'), [])
        self.assertEqual(len(pmod.live_hits(['a/**'], holding)), 1)

    def test_a_pull_on_the_exempt_branch_is_no_hit(self):
        holding = [self.pulled('SIB', 'a/x.py', by=BRANCH), self.pulled('OTHER', 'a/y.py')]
        hits = pmod.live_hits(['a/**'], holding, branch=BRANCH)
        self.assertEqual(len(hits), 1, hits)
        self.assertIn('OTHER', hits[0])


class Widened(unittest.TestCase):
    """pmod.widened_members: the new members no old member covers."""

    def check(self, old, new, want):
        self.assertEqual(pmod.widened_members(old, new), want, (old, new))

    def test_cover_rules(self):
        self.check(['a/**'], ['a/b.py'], [])
        self.check(['a/**'], ['a/**'], [])
        self.check(['a/**'], ['a/b/**'], [])
        self.check(['a/b.py'], ['a/**'], ['a/**'])
        self.check(['a/b.py'], ['a/b.py', 'c.py'], ['c.py'])
        self.check(['A/B.py'], ['a/b.py'], [])
        self.check(['none'], ['a.py'], ['a.py'])
        self.check(['a.py'], ['none'], [])
        self.check(['a'], ['a/b.py'], ['a/b.py'])
        self.check(['a/**'], ['a/*.py'], ['a/*.py'])


class TakeoverCollision(StoreCase):
    """The dead-pull takeover is the same door: a takeover that collides with a live pull needs
    `--ruling`."""
    remote = False

    def setUp(self):
        super().setUp()
        self.store.session = 'kit'
        self.repo.git('branch', 'kit/live-one')

    def pulled(self, rid, by, scope, session='kit'):
        return priced(rid, state='pulled', pulled_at='2026-10-01', pulled_by=by, scope=scope,
                      session=session)

    def test_a_colliding_takeover_is_refused_without_a_ruling(self):
        self.seed(self.pulled('DEAD', 'kit/gone', 'area/shared/**'),
                  self.pulled('LIVE', 'kit/live-one', 'area/shared/x.py', session='other'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'DEAD')
        self.assertIn('LIVE (in flight on kit/live-one)', str(cm.exception))
        self.assertIn('--ruling R-nnnn', str(cm.exception))
        self.assertEqual(self.get('DEAD').get('pulled_by'), 'kit/gone')

    def test_a_colliding_takeover_with_a_ruling_is_written(self):
        self.seed(self.pulled('DEAD', 'kit/gone', 'area/shared/**'),
                  self.pulled('LIVE', 'kit/live-one', 'area/shared/x.py', session='other'))
        change = self.verb('pull', 'DEAD', ruling='R-9001')
        self.assertEqual(self.get('DEAD').get('pulled_by'), BRANCH)
        self.assertEqual(change.ruling, 'R-9001')

    def test_a_colliding_takeover_with_a_bad_ruling_shape_is_usage(self):
        self.seed(self.pulled('DEAD', 'kit/gone', 'area/shared/**'),
                  self.pulled('LIVE', 'kit/live-one', 'area/shared/x.py', session='other'))
        with self.assertRaises(Usage):
            self.verb('pull', 'DEAD', ruling='9001')

    def test_a_takeover_that_overlaps_a_pull_on_the_callers_own_branch_needs_no_ruling(self):
        self.seed(self.pulled('DEAD', 'kit/gone', 'area/shared/**'),
                  self.pulled('SIB', BRANCH, 'area/shared/x.py'))
        change = self.verb('pull', 'DEAD')
        self.assertEqual(self.get('DEAD').get('pulled_by'), BRANCH)
        self.assertIsNone(change.ruling)


if __name__ == '__main__':
    unittest.main()
