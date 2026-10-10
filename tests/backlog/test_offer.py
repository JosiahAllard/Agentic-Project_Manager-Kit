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
        s = snap(priced('AAA', scope='binding/**'), priced('BBB', scope='worker/**'),
                 priced('HOLD', state='pulled', pulled_at='2026-10-01', pulled_by='b',
                        scope='binding/src/X.java'))
        ids, passed = offer_ids(s, holding=[s.get('HOLD')])
        self.assertEqual(ids, ['BBB'])
        self.assertIn('collides', passed['AAA'])

    def test_offers_do_not_collide_with_each_other(self):
        s = snap(priced('AAA', priority='1', scope='binding/**'),
                 priced('BBB', priority='2', scope='binding/x/**'),
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
        s = snap(priced('AAA', priority='1', scope='binding/**', session='gamma'),
                 priced('BBB', priority='2', scope='binding/x/**', session='kit'),
                 priced('CCC', priority='3', scope='worker/**', session='kit'))
        ids, passed = offer_ids(s, session='kit')
        self.assertEqual(ids, ['CCC'])
        self.assertIn('AAA', passed['BBB'])
        self.assertNotIn('AAA', passed)
        self.assertIn('session `gamma`', omod.not_offered_why(s.get('AAA'), [], 'kit'))

    def test_a_seed_goes_first_and_takes_its_scope(self):
        s = snap(priced('AAA', priority='1', scope='binding/**'),
                 priced('BBB', priority='5', scope='binding/x/**'))
        (offers, _), _, _ = omod.next_offer(s, [], seeds=['BBB'])
        self.assertEqual([(o.rec.id, o.seeded) for o in offers], [('BBB', True)])

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


if __name__ == '__main__':
    unittest.main()
