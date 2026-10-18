"""The partition, the rank order and the offer (ported from earlier per-repository backlog tests:
Partition, Rank and NextOffer cases, `requires:` among them), on snapshots built in memory."""
import unittest

from kitlib.backlog import offer as omod
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod

from tests.backlog.helpers import priced, rec


def snap(*records, rules=None):
    texts = {}
    for r in records:
        texts.update(r)
    rules = rules or rmod.Rules('upper', ('L0', 'L1'))
    parsed = [rmod.parse('backlog/%s.md' % stem, text, stem) for stem, text in texts.items()]
    return smod.Snapshot(parsed, 'a' * 40, 'test', rules)


def offer_ids(s, holding=(), **kw):
    (offers, passed), refusals, _ranks = omod.next_offer(s, list(holding), **kw)
    assert not refusals, refusals
    return [o.rec.id for o in offers], {p.rec.id: p.why for p in passed}


class Partition(unittest.TestCase):

    def test_every_record_lands_in_exactly_one_bucket(self):
        s = snap(priced('AAA'), rec('BBB'), rec('CCC', state='blocked', blocked_on='live-client'),
                 rec('DDD', state='parked'), priced('EEE', priority='-1', priority_by='owner',
                                                    owner_ask='which one?'),
                 priced('FFF', state='pulled', pulled_at='2026-10-01', pulled_by='b'),
                 rec('GGG', state='weird'), {'HHH': 'not a record\n'})
        b = omod.Banner(s)
        got = {k: [r.stem for r in v] for k, v in b.buckets.items() if v}
        self.assertEqual(got, {omod.PULLABLE: ['AAA'], omod.UNPRIORITISED_OPEN: ['BBB'],
                               omod.HELD: ['CCC'], omod.PARKED: ['DDD'],
                               omod.OWNER_PRIORITISED: ['EEE'], omod.IN_FLIGHT: ['FFF'],
                               omod.UNCLASSIFIED: ['GGG'], omod.NOT_READ: ['HHH']})
        line = b.lines()[0]
        self.assertIn('8 records', line)
        self.assertIn('1 pullable', line)
        self.assertIn('1 parked', line)

    def test_held_groups_count_each_record_once(self):
        s = snap(rec('AAA', state='blocked', blocked_on=['owner', 'live-client'], owner_ask='q?'),
                 rec('BBB', state='blocked', blocked_on='live-client'))
        groups, multi = omod.Banner(s).held_groups()
        self.assertEqual(sum(n for _, n in groups), 2)
        self.assertEqual(multi, 1)

    def test_the_queue_warning(self):
        self.assertIn('QUEUE DEPTH: 1 pullable', omod.Banner(snap(priced('AAA'))).queue_warning())
        many = [priced('R%02d' % i, priority='1' if i < 3 else '4') for i in range(10)]
        self.assertIsNone(omod.Banner(snap(*many)).queue_warning())


class Rank(unittest.TestCase):

    def test_preempt_then_pin_then_priority_then_effort_then_id(self):
        s = snap(priced('AAA', priority='3'), priced('BBB', priority='1', effort='l'),
                 priced('CCC', priority='1', effort='s'), priced('DDD', priority='5', pin='1'),
                 priced('EEE', priority='5', preempt='because reasons', preempt_by='b',
                        preempt_at='2026-10-01'),
                 priced('FFF', priority='1', effort='s'))
        ranks = omod.ranked(s)
        self.assertEqual(sorted(ranks, key=ranks.get), ['EEE', 'DDD', 'CCC', 'FFF', 'BBB', 'AAA'])

    def test_unpriced_records_have_no_rank(self):
        s = snap(rec('AAA'), priced('BBB'))
        self.assertEqual(omod.ranked(s), {'BBB': 1})
        self.assertEqual(omod.rank_label(s.get('AAA'), omod.ranked(s)), '-')


class NextOffer(unittest.TestCase):

    def test_the_offer_is_rank_order_up_to_the_cap(self):
        s = snap(*[priced('R%d' % i, priority=str(1 + i % 5)) for i in range(8)])
        ids, passed = offer_ids(s, cap=3)
        self.assertEqual(ids, ['R0', 'R5', 'R1'])
        self.assertEqual(set(passed), {'R2', 'R3', 'R4', 'R6', 'R7'})
        self.assertTrue(all('cap' in why for why in passed.values()))

    def test_only_pullable_records_are_offered(self):
        s = snap(priced('AAA'), rec('BBB'), rec('CCC', state='parked', priority='1',
                                                 priority_by='agent'),
                 priced('DDD', state='blocked', blocked_on='live-client'))
        self.assertEqual(offer_ids(s)[0], ['AAA'])

    def test_a_live_pull_occupies_its_scope(self):
        s = snap(priced('AAA', scope='link/**'), priced('BBB', scope='worker/**'),
                 priced('HOLD', state='pulled', pulled_at='2026-10-01', pulled_by='b',
                        scope='link/src/X.java'))
        ids, passed = offer_ids(s, holding=[s.get('HOLD')])
        self.assertEqual(ids, ['BBB'])
        self.assertIn('collides', passed['AAA'])

    def test_offers_do_not_collide_with_each_other(self):
        s = snap(priced('AAA', priority='1', scope='link/**'),
                 priced('BBB', priority='2', scope='link/x/**'),
                 priced('CCC', priority='3', scope='worker/**'))
        ids, passed = offer_ids(s)
        self.assertEqual(ids, ['AAA', 'CCC'])
        self.assertIn('collides', passed['BBB'])

    def test_an_undecidable_scope_is_a_collision_never_a_clearance(self):
        s = snap(priced('AAA', scope='*.java'), priced('BBB', scope='worker/**'))
        ids, passed = offer_ids(s)
        self.assertEqual(ids, ['BBB'])
        self.assertIn('undecidable', passed['AAA'])

    def test_an_undecidable_live_pull_refuses_the_offer(self):
        s = snap(priced('HOLD', state='pulled', pulled_at='2026-10-01', pulled_by='b',
                        scope='*.java'), priced('AAA'))
        result, refusals, _ = omod.next_offer(s, [s.get('HOLD')])
        self.assertIsNone(result)
        self.assertTrue(refusals)

    def test_session_filter_and_another_sessions_scope(self):
        s = snap(priced('AAA', priority='1', scope='link/**', session='gamma'),
                 priced('BBB', priority='2', scope='link/x/**', session='kit'),
                 priced('CCC', priority='3', scope='worker/**', session='kit'))
        ids, passed = offer_ids(s, session='kit')
        self.assertEqual(ids, ['CCC'])
        self.assertIn('AAA', passed['BBB'])
        self.assertNotIn('AAA', passed)
        self.assertIn('session `gamma`', omod.not_offered_why(s.get('AAA'), [], 'kit'))

    def test_a_seed_goes_first_and_takes_its_scope(self):
        s = snap(priced('AAA', priority='1', scope='link/**'),
                 priced('BBB', priority='5', scope='link/x/**'))
        (offers, _), _, _ = omod.next_offer(s, [], seeds=['BBB'])
        self.assertEqual([(o.rec.id, o.seeded) for o in offers], [('BBB', True)])

    def test_a_seed_that_collides_with_a_live_pull_is_passed_over(self):
        s = snap(priced('BBB', priority='5', scope='link/x/**'),
                 priced('HOLD', state='pulled', pulled_at='2026-10-01', pulled_by='b',
                        scope='link/**'))
        (offers, passed), _, _ = omod.next_offer(s, [s.get('HOLD')], seeds=['BBB'])
        self.assertEqual([o.rec.id for o in offers], [])
        self.assertIn('already taken by HOLD', {p.rec.id: p.why for p in passed}['BBB'])

    def test_an_unmet_requires_is_passed_over(self):
        s = snap(priced('AAA', priority='1', requires='item:BBB'), priced('BBB', priority='2'))
        ids, passed = offer_ids(s, unmet=lambda r: [('item:BBB', 'open')] if r.id == 'AAA' else [])
        self.assertEqual(ids, ['BBB'])
        self.assertIn('requires item:BBB', passed['AAA'])

    def test_why_a_record_is_not_offered(self):
        s = snap(rec('AAA', state='parked'), rec('BBB'),
                 rec('CCC', state='blocked', blocked_on='live-client'))
        self.assertEqual(omod.not_offered_why(s.get('AAA'), []), 'it is parked')
        self.assertIn('unprioritised', omod.not_offered_why(s.get('BBB'), []))
        self.assertIn('live-client', omod.not_offered_why(s.get('CCC'), []))


class Requires(unittest.TestCase):

    def test_item_met_only_when_closed_in_history(self):
        s = snap(priced('AAA', requires=['item:BBB', 'item:CCC', 'item:DDD', 'just a note']),
                 priced('BBB'))
        hist = {'CCC': 'abc1234 closes CCC: done and dusted', 'DDD': 'abc1234 retires DDD: no'}.get
        got = omod.unmet_requires(s.get('AAA'), s, hist, '.', 'main')
        self.assertEqual([m for m, _ in got], ['item:BBB', 'item:DDD'])


class CollisionHolder(unittest.TestCase):
    """A collision names who holds the scope; a pulled record's narrowed `scope:` lines are what
    it holds."""

    def hold(self, scope):
        return priced('HOLD', state='pulled', pulled_at='2026-10-01', pulled_by='kit/holder',
                      scope=scope)

    def test_a_collision_with_a_live_pull_names_the_holder_and_its_branch(self):
        s = snap(priced('AAA', scope='kitlib/backlog/**'), self.hold('kitlib/backlog/offer.py'))
        _ids, passed = offer_ids(s, holding=[s.get('HOLD')])
        self.assertEqual(passed['AAA'], 'scope kitlib/backlog/** collides with '
                         'kitlib/backlog/offer.py already taken by HOLD (in flight on kit/holder)')

    def test_a_narrowed_live_pull_clears_a_disjoint_record(self):
        wide = snap(priced('AAA', scope='kitlib/backlog/scope.py'), self.hold('kitlib/backlog/**'))
        ids, passed = offer_ids(wide, holding=[wide.get('HOLD')])
        self.assertEqual(ids, [])
        self.assertIn('already taken by HOLD (in flight on kit/holder)', passed['AAA'])
        narrowed = snap(priced('AAA', scope='kitlib/backlog/scope.py'),
                        self.hold('kitlib/backlog/offer.py'))
        ids, passed = offer_ids(narrowed, holding=[narrowed.get('HOLD')])
        self.assertEqual(ids, ['AAA'])
        self.assertNotIn('AAA', passed)

    def test_another_sessions_holder_is_still_named_by_session(self):
        s = snap(priced('AAA', priority='1', scope='binding/**', session='gamma'),
                 priced('BBB', priority='2', scope='binding/x/**', session='kit'))
        _ids, passed = offer_ids(s, session='kit')
        self.assertEqual(passed['BBB'], 'scope binding/x/** collides with binding/** already '
                         'taken by AAA (session gamma)')

    def test_a_record_offered_above_in_the_same_pass_is_named(self):
        s = snap(priced('AAA', priority='1', scope='binding/**'),
                 priced('BBB', priority='2', scope='binding/x/**'),
                 priced('CCC', priority='3', scope='worker/**'))
        ids, passed = offer_ids(s)
        self.assertEqual(ids, ['AAA', 'CCC'])
        self.assertEqual(passed['BBB'], 'scope binding/x/** collides with binding/** already '
                         'taken by AAA (offered above)')

    def test_a_seed_offered_first_is_named_as_offered_above(self):
        s = snap(priced('AAA', priority='1', scope='binding/**'),
                 priced('BBB', priority='5', scope='binding/x/**'))
        ids, passed = offer_ids(s, seeds=['BBB'])
        self.assertEqual(ids, ['BBB'])
        self.assertEqual(passed['AAA'], 'scope binding/** collides with binding/x/** already '
                         'taken by BBB (offered above)')

    def test_a_pull_with_no_branch_held_by_a_failed_probe_is_named_with_a_question_mark(self):
        from unittest import mock
        s = snap(priced('AAA', scope='kitlib/backlog/**'),
                 priced('HOLD', state='pulled', pulled_at='2026-10-01',
                        scope='kitlib/backlog/offer.py'))
        with mock.patch.object(omod, 'live_branches', return_value=(None, 'git failed')):
            holding, dead, why = omod.in_flight_split(s, '.', 'origin')
        self.assertEqual(([r.id for r in holding], dead, why), (['HOLD'], [], 'git failed'))
        _ids, passed = offer_ids(s, holding=holding)
        self.assertEqual(passed['AAA'], 'scope kitlib/backlog/** collides with '
                         'kitlib/backlog/offer.py already taken by HOLD (in flight on ?)')


class BlockerSummary(unittest.TestCase):
    """`blocker_summary` ranks the live pulls and other sessions' records by how many passed-over
    records they block."""

    def passed_for(self, s, holding=(), **kw):
        (_offers, passed), _refusals, _ranks = omod.next_offer(s, list(holding), **kw)
        return passed

    def test_the_pull_blocking_most_records_comes_first(self):
        hold_a = priced('HOLDA', state='pulled', pulled_at='2026-10-01', pulled_by='kit/a',
                        scope='a/**')
        hold_b = priced('HOLDB', state='pulled', pulled_at='2026-10-01', pulled_by='kit/b',
                        scope='b/**')
        s = snap(hold_a, hold_b, priced('R1', priority='1', scope='b/x'),
                 priced('R2', priority='2', scope='b/y'), priced('R3', priority='3', scope='a/x'))
        passed = self.passed_for(s, holding=[s.get('HOLDA'), s.get('HOLDB')])
        self.assertEqual(omod.blocker_summary(passed),
                         ['blocks 2: kit/b (HOLDB)', 'blocks 1: kit/a (HOLDA)'])

    def test_another_session_counts_and_an_offered_record_does_not(self):
        s = snap(priced('AAA', priority='1', scope='binding/**', session='gamma'),
                 priced('BBB', priority='2', scope='binding/x/**', session='kit'),
                 priced('CCC', priority='3', scope='w/**', session='kit'),
                 priced('DDD', priority='4', scope='w/y', session='kit'))
        passed = self.passed_for(s, session='kit')
        self.assertEqual(omod.blocker_summary(passed), ['blocks 1: session gamma (AAA)'])

    def test_it_keeps_the_top_few_and_is_empty_with_no_collision(self):
        self.assertEqual(omod.blocker_summary([]), [])
        recs = [priced('H%d' % i, state='pulled', pulled_at='2026-10-01', pulled_by='kit/h%d' % i,
                       scope='h%d/**' % i) for i in range(7)]
        recs += [priced('R%d' % i, priority='1', scope='h%d/x' % i) for i in range(7)]
        s = snap(*recs)
        passed = self.passed_for(s, holding=[s.get('H%d' % i) for i in range(7)])
        self.assertEqual(len(omod.blocker_summary(passed)), 5)
        # H0..H6 block 1, 2, 3, 1, 2, 3, 1 records: top five by count, ties by name
        more = [priced('X%d' % i, priority='1', scope=sc) for i, sc in
                enumerate(['h1/y', 'h2/y', 'h2/z', 'h5/y', 'h5/z', 'h6/y'])]
        s = snap(*(recs + more))
        passed = self.passed_for(s, holding=[s.get('H%d' % i) for i in range(7)])
        self.assertEqual(omod.blocker_summary(passed),
                         ['blocks 3: kit/h2 (H2)', 'blocks 3: kit/h5 (H5)', 'blocks 2: kit/h1 (H1)',
                          'blocks 2: kit/h6 (H6)', 'blocks 1: kit/h0 (H0)'])

if __name__ == '__main__':
    unittest.main()
