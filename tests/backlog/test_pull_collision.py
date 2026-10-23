"""A pull whose scope collides with a live pull is the `pull-collision` row
(kitlib/backlog/roles.py): refused unless the caller's role may take it (`may_collide`), and
`--reason` alone is never enough. `set --scope` on a pulled record runs the same check when it
widens the scope (`set-widen-collision`), and narrowing stays free, so `set` is not a second door
around the pull check. A taken collision names its row on the Change and marks its subject
(policy.COLLISION_MARK). End to end through Store.write on a no-remote ref store."""
import unittest

from kitlib.backlog import audit_check
from kitlib.backlog import plan as pmod
from kitlib.backlog import policy
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Failure, Refusal, Usage

from tests.backlog.helpers import BRANCH, TODAY, StoreCase, parse, priced, rec

HOLDER = 'kit/holder'


def hold(scope='kitlib/backlog/**', rid='HOLD', by=HOLDER):
    return priced(rid, state='pulled', pulled_at=TODAY, pulled_by=by, scope=scope)


def mine(scope, rid='MINE'):
    return priced(rid, state='pulled', pulled_at=TODAY, pulled_by=BRANCH, scope=scope)


def took(case, change, row):
    """`change` took the collision row `row`: named on it, and marked on its subject."""
    case.assertEqual(getattr(change, 'rows', None), [row])
    case.assertTrue(change.subject.endswith(policy.COLLISION_MARK), change.subject)


def took_none(case, change):
    case.assertFalse(getattr(change, 'rows', None))
    case.assertNotIn(policy.COLLISION_MARK, change.subject)


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
        self.assertIn('`pull-collision`', msg)
        self.assertNotIn('--ruling', msg)
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_a_colliding_pull_with_no_reason_names_the_row_too(self):
        self.seed(hold(), priced('FOO', scope='kitlib/backlog/scope.py'))
        for may in (False, True):
            with self.subTest(may_collide=may):
                with self.assertRaises(Refusal) as cm:
                    self.verb('pull', 'FOO', may_collide=may)
                self.assertIn('already taken by HOLD (in flight on %s)' % HOLDER,
                              str(cm.exception))
                self.assertIn('`pull-collision`', str(cm.exception))
                self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_a_colliding_pull_the_role_may_take_is_written_and_names_the_row(self):
        self.seed(hold(), priced('FOO', scope='kitlib/backlog/scope.py'))
        change = self.verb('pull', 'FOO', reason='the owner wants it today', may_collide=True)
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('preempt')),
                         ('pulled', 'the owner wants it today'))
        took(self, change, 'pull-collision')
        self.assertIsNone(change.ruling)

    def test_a_collision_with_a_dead_pull_takes_no_row(self):
        self.seed(hold(by='kit/gone'), priced('FOO', scope='kitlib/backlog/scope.py'))
        change = self.verb('pull', 'FOO')
        self.assertEqual(self.get('FOO').get('state'), 'pulled')
        took_none(self, change)

    def test_an_out_of_order_pull_with_no_collision_still_takes_a_reason_alone(self):
        self.seed(hold(), priced('TOP', priority='1', scope='docs/**'),
                  priced('FOO', priority='5', scope='docs/x.md'))
        self.verb('pull', 'FOO', reason='needed before the others')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('preempt')), ('pulled', 'needed before the others'))

    def test_a_minus_one_record_that_also_collides_is_never_pulled(self):
        # pull-held refuses every role: a role that may collide does not release the hold by
        # pulling it; the owner or the PM releases it with `set` first (docs/backlog.md "Owner
        # gates")
        self.seed(hold(), rec('MIN', priority='-1', owner_ask='which?',
                              scope='kitlib/backlog/scope.py'))
        for may in (False, True):
            with self.assertRaises(Refusal) as cm:
                self.verb('pull', 'MIN', reason='the owner said go', may_collide=may)
            self.assertIn('set MIN --priority', str(cm.exception))
            r = self.get('MIN')
            self.assertEqual((r.get('state'), r.get('priority'), r.get('hold_ruling')),
                             ('open', '-1', None))

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
        took_none(self, change)
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
        self.assertIn('`pull-collision`', str(cm.exception))
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_the_offer_still_ranks_records_the_branch_does_not_hold(self):
        # Only the caller's own pulls are left out: a higher-ranked open record still comes first.
        self.seed(mine('kitlib/backlog/plan.py'), priced('TOP', priority='1', scope='docs/**'),
                  priced('FOO', priority='5', scope='docs/x.md'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('TOP (offered above)', str(cm.exception))

    def seed_x_y(self):
        # The branch holds a/**. X (rank 1) overlaps it and docs/y.md; Y (rank 2) is docs/y.md.
        # `next` without the branch passes X over (a/x.py is taken) and offers Y; `next` as the
        # branch offers X and passes Y over (X is offered above).
        self.seed(mine('a/**'), priced('X', priority='1', scope=['a/x.py', 'docs/y.md']),
                  priced('Y', priority='2', scope='docs/y.md'))

    def test_a_record_next_offers_without_the_branch_pulls_plainly(self):
        # Y is in `next`'s offer as the coordinator in the main checkout reads it, so pulling it
        # onto the branch must not count X, which that `next` passed over, as offered above.
        self.seed_x_y()
        change = self.verb('pull', 'Y')
        r = self.get('Y')
        self.assertEqual((r.get('state'), r.get('preempt')), ('pulled', None))
        took_none(self, change)

    def test_a_record_next_offers_as_the_branch_pulls_plainly(self):
        self.seed_x_y()
        self.verb('pull', 'X')
        self.assertEqual((self.get('X').get('state'), self.get('X').get('preempt')),
                         ('pulled', None))

    def test_the_offer_next_prints_as_the_branch_leaves_its_own_pulls_out(self):
        self.seed_x_y()
        snap = self.store.load()
        offers, passed, _holding, own = pmod.offer_for(snap, self.ctx)
        self.assertEqual(own, BRANCH)
        self.assertEqual([o.rec.id for o in offers], ['X'])
        self.assertEqual([p.rec.id for p in passed], ['Y'])
        self.ctx.branch = 'main'                      # a main never bundles: its pulls hold scope
        offers, passed, _holding, own = pmod.offer_for(snap, self.ctx)
        self.assertIsNone(own)
        self.assertEqual([o.rec.id for o in offers], ['Y'])

    def assert_still_collides(self, by):
        if by != BRANCH:
            self.repo.git('branch', by)
        self.seed(hold(scope='kitlib/backlog/**', by=by),
                  priced('FOO', scope='kitlib/backlog/scope.py'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('HOLD (in flight on %s)' % by, str(cm.exception))
        self.assertIn('`pull-collision`', str(cm.exception))
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_a_branch_that_shares_the_callers_prefix_still_collides(self):
        self.assert_still_collides(BRANCH + '-two')

    def test_a_branch_the_caller_names_by_its_prefix_still_collides(self):
        self.ctx.branch = 'tools/some'
        self.assert_still_collides(BRANCH)

    def test_the_callers_branch_in_another_letter_case_still_collides(self):
        # pulled_by is compared exactly: the branch upper-cased is not the branch that pulled.
        self.ctx.branch = BRANCH.upper()
        self.seed(mine('kitlib/backlog/**'), priced('FOO', scope='kitlib/backlog/scope.py'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('MINE (in flight on %s)' % BRANCH, str(cm.exception))
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_the_callers_branch_spelt_as_a_ref_still_collides(self):
        self.ctx.branch = 'refs/heads/' + BRANCH
        self.seed(mine('kitlib/backlog/**'), priced('FOO', scope='kitlib/backlog/scope.py'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('MINE (in flight on %s)' % BRANCH, str(cm.exception))
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_the_offer_leaves_out_only_the_branch_spelt_exactly(self):
        # Direct, so a refusal from another check cannot pass it: as BRANCH, X is offered (its
        # a/x.py overlaps only the branch's own MINE); as any other spelling, MINE holds a/** and
        # X is passed over, so a case-insensitive or prefix match would offer X here.
        self.seed_x_y()
        snap = self.store.load()
        for name in (BRANCH.upper(), 'tools/some', BRANCH + '-two', 'refs/heads/' + BRANCH):
            self.ctx.branch = name
            offers, passed, _holding, own = pmod.offer_for(snap, self.ctx)
            self.assertEqual(own, name)
            self.assertEqual([o.rec.id for o in offers], ['Y'], name)
            self.assertEqual([p.rec.id for p in passed], ['X'], name)

    def test_a_detached_head_names_no_branch_of_its_own(self):
        self.repo.git('checkout', '-b', 'tools/other')
        self.ctx.branch = None
        self.assertEqual(pmod.own_branch(self.ctx), 'tools/other')   # the control: HEAD is read
        self.repo.git('checkout', '--detach')
        self.assertIsNone(pmod.own_branch(self.ctx))
        self.assertEqual(pmod.own_branch(self.ctx, 'tools/named'), 'tools/named')

    def test_a_pull_takes_what_next_offers_as_the_branch_when_its_own_pull_has_a_bad_scope(self):
        # `next` as the branch leaves its own pulls out, so their scopes are never read there; the
        # plain offer `pull` also consults reads them all. A scope that cannot be compared must
        # not make `pull` refuse a record `next` offered.
        self.seed(mine('a/*.py'), priced('Y', scope='docs/y.md'))
        snap = self.store.load()
        offers, _passed, _holding, own = pmod.offer_for(snap, self.ctx)
        self.assertEqual((own, [o.rec.id for o in offers]), (BRANCH, ['Y']))
        change = self.verb('pull', 'Y')
        self.assertEqual((self.get('Y').get('state'), self.get('Y').get('preempt')),
                         ('pulled', None))
        took_none(self, change)
        note = [ln for ln in change.lines if ln.startswith('NOTE: the offer without branch')]
        self.assertEqual(len(note), 1, change.lines)
        self.assertIn(BRANCH, note[0])
        self.assertIn('`scope: a/*.py`', note[0])

    def test_a_plain_pull_onto_the_branch_prints_no_offer_note(self):
        self.seed(mine('a/**'), priced('Y', scope='docs/y.md'))
        change = self.verb('pull', 'Y')
        self.assertFalse([ln for ln in change.lines if 'offer without branch' in ln],
                         change.lines)

    def test_a_bad_scope_on_another_branchs_pull_fails_the_pull(self):
        self.seed(hold(scope='a/*.py'), priced('Y', scope='docs/y.md'))
        with self.assertRaises(Failure) as cm:
            self.verb('pull', 'Y')
        msg = str(cm.exception)
        self.assertTrue(msg.startswith('`next` cannot compute an offer: '), msg)
        self.assertIn('`scope: a/*.py`', msg)
        self.assertEqual(self.get('Y').get('state'), 'open')


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
        self.assertIn('`set-widen-collision`', msg)
        self.assertNotIn('--ruling', msg)
        self.assertEqual(self.get('MINE').all('scope'), ['kitlib/backlog/plan.py'])

    def test_widening_into_a_live_pull_the_role_may_take_is_written_and_names_the_row(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/plan.py'))
        change = self.verb('set', 'MINE', [('scope', ['kitlib/backlog/**'])], may_collide=True)
        self.assertEqual(self.get('MINE').all('scope'), ['kitlib/backlog/**'])
        took(self, change, 'set-widen-collision')
        self.assertIsNone(change.ruling)

    def test_widening_into_free_space_is_free(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/plan.py'))
        for may in (False, True):
            with self.subTest(may_collide=may):
                change = self.verb('set', 'MINE', [('scope', ['kitlib/backlog/plan.py',
                                                              'docs/backlog.md'])],
                                   may_collide=may)
                self.assertEqual(self.get('MINE').all('scope'),
                                 ['kitlib/backlog/plan.py', 'docs/backlog.md'])
                took_none(self, change)
                self.verb('set', 'MINE', [('scope', ['kitlib/backlog/plan.py'])])

    def test_the_widen_refusal_names_the_repeatable_scope_form(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/plan.py'))
        with self.assertRaises(Refusal) as cm:
            self.verb('set', 'MINE', [('scope', ['kitlib/backlog/plan.py', 'kitlib/backlog/**'])])
        self.assertIn('`set MINE --scope <path> [--scope <path> ...]` (the whole new list)',
                      str(cm.exception))

    def test_a_narrowing_or_an_unchanged_scope_takes_no_row(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/**'))
        for new in (['kitlib/backlog/**'], ['kitlib/backlog/plan.py']):
            with self.subTest(scope=new):
                change = self.verb('set', 'MINE', [('scope', new)], may_collide=True)
                self.assertEqual(self.get('MINE').all('scope'), new)
                took_none(self, change)

    def test_a_comma_joined_scope_is_usage_naming_one_scope_per_path(self):
        # Not a collision with every live pull: a malformed member is the caller's mistake.
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/plan.py'))
        with self.assertRaises(Usage) as cm:
            self.verb('set', 'MINE', [('scope', ['kitlib/backlog/plan.py,docs/backlog.md'])])
        msg = str(cm.exception)
        self.assertIn('bad `--scope kitlib/backlog/plan.py,docs/backlog.md`', msg)
        self.assertIn('one `--scope <path>` per path', msg)
        self.assertNotIn('live pull', msg)
        self.assertEqual(self.get('MINE').all('scope'), ['kitlib/backlog/plan.py'])

    def test_narrowing_stays_free_even_when_the_old_scope_collides(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/**'))
        self.verb('set', 'MINE', [('scope', ['kitlib/backlog/offer.py'])])
        self.assertEqual(self.get('MINE').all('scope'), ['kitlib/backlog/offer.py'])

    def test_an_open_record_widens_freely(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), priced('FOO', scope='docs/x.md'))
        self.verb('set', 'FOO', [('scope', ['kitlib/backlog/**'])])
        self.assertEqual(self.get('FOO').all('scope'), ['kitlib/backlog/**'])

    def test_widening_never_collides_with_the_record_itself(self):
        # MINE is a live pull too; its own old scope is under the new one.
        self.seed(mine('kitlib/backlog/plan.py'))
        self.verb('set', 'MINE', [('scope', ['kitlib/backlog/**'])])
        self.assertEqual(self.get('MINE').all('scope'), ['kitlib/backlog/**'])

    def test_widening_into_a_sibling_on_the_same_branch_is_free(self):
        self.seed(mine('kitlib/backlog/offer.py', rid='SIB'), mine('kitlib/backlog/plan.py'))
        change = self.verb('set', 'MINE', [('scope', ['kitlib/backlog/**'])])
        self.assertEqual(self.get('MINE').all('scope'), ['kitlib/backlog/**'])
        took_none(self, change)

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
    """The dead-pull takeover is the same door: a takeover that collides with a live pull is the
    `pull-collision` row."""
    remote = False

    def setUp(self):
        super().setUp()
        self.store.session = 'kit'
        self.repo.git('branch', 'kit/live-one')

    def pulled(self, rid, by, scope, session='kit'):
        return priced(rid, state='pulled', pulled_at='2026-10-01', pulled_by=by, scope=scope,
                      session=session)

    def test_a_colliding_takeover_is_refused_unless_the_role_may_collide(self):
        self.seed(self.pulled('DEAD', 'kit/gone', 'area/shared/**'),
                  self.pulled('LIVE', 'kit/live-one', 'area/shared/x.py', session='other'))
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'DEAD')
        self.assertIn('LIVE (in flight on kit/live-one)', str(cm.exception))
        self.assertIn('`pull-collision`', str(cm.exception))
        self.assertEqual(self.get('DEAD').get('pulled_by'), 'kit/gone')

    def test_a_colliding_takeover_the_role_may_take_is_written_and_names_the_row(self):
        self.seed(self.pulled('DEAD', 'kit/gone', 'area/shared/**'),
                  self.pulled('LIVE', 'kit/live-one', 'area/shared/x.py', session='other'))
        change = self.verb('pull', 'DEAD', may_collide=True)
        self.assertEqual(self.get('DEAD').get('pulled_by'), BRANCH)
        took(self, change, 'pull-collision')

    def test_a_takeover_that_overlaps_a_pull_on_the_callers_own_branch_takes_no_row(self):
        self.seed(self.pulled('DEAD', 'kit/gone', 'area/shared/**'),
                  self.pulled('SIB', BRANCH, 'area/shared/x.py'))
        change = self.verb('pull', 'DEAD')
        self.assertEqual(self.get('DEAD').get('pulled_by'), BRANCH)
        took_none(self, change)


class AuditInference(StoreCase):
    """kitlib/backlog/audit_check.py reads the collision row back from a published store commit:
    no flag names it, so the subject's policy.COLLISION_MARK is the only trace an audit has."""
    remote = False

    def setUp(self):
        super().setUp()
        self.repo.git('branch', HOLDER)

    def rows(self, verb):
        """audit_check's rows for the store's newest commit, as the audit reads them."""
        newest = audit_check.history(self.repo.root, smod.STORE_REF, smod.STORE_DIR, 0)[0]
        return audit_check.rows_of(self.repo.root, verb, newest)

    def test_a_taken_pull_collision_is_read_back(self):
        self.seed(hold(), priced('FOO', scope='kitlib/backlog/scope.py'))
        self.verb('pull', 'FOO', reason='the owner wants it today', may_collide=True)
        self.assertIn('pull-collision', self.rows('pull'))

    def test_a_plain_pull_reads_back_no_collision(self):
        self.seed(hold(), priced('FOO', scope='docs/other.md'))
        self.verb('pull', 'FOO', may_collide=True)
        self.assertEqual(self.rows('pull'), ['pull'])

    def test_a_taken_widen_collision_is_read_back(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/plan.py'))
        self.verb('set', 'MINE', [('scope', ['kitlib/backlog/**'])], may_collide=True)
        self.assertIn('set-widen-collision', self.rows('set'))

    def test_a_free_widen_reads_back_no_collision(self):
        self.seed(hold(scope='kitlib/backlog/offer.py'), mine('kitlib/backlog/plan.py'))
        self.verb('set', 'MINE', [('scope', ['kitlib/backlog/plan.py', 'docs/backlog.md'])],
                  may_collide=True)
        self.assertNotIn('set-widen-collision', self.rows('set'))


if __name__ == '__main__':
    unittest.main()
