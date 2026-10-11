"""Parked records: `set-status ID parked --q Q-nnn` parks a record on an owner
question, `unpark ID --ruling R-nnnn` opens it again with a `resume` sort key that ranks above
priority and below a pin, and a parked record never takes a place in the offer's cap."""
import unittest

from kitlib.backlog import offer as omod
from kitlib.backlog import plan as pmod
from kitlib.backlog import record as rmod
from kitlib.backlog.errors import Refusal, Usage

from tests.backlog.helpers import BRANCH, TODAY, StoreCase, messages, priced, rec
from tests.backlog.test_offer import offer_ids, snap

PARKED = dict(state='parked', q='Q-001', parked_at='2026-10-01')


class Park(StoreCase):
    remote = False

    def test_parking_an_open_record_records_the_question_and_the_date(self):
        self.seed(priced('FOO'))
        change = self.verb('set_status', 'FOO', 'parked', 'Q-001')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('q'), r.get('parked_at')),
                         ('parked', 'Q-001', TODAY))
        self.assertIn('park FOO', change.subject)
        self.assertEqual(omod.classify(r)[0], omod.PARKED)

    def test_parking_a_pulled_record_gives_up_the_pull(self):
        self.seed(priced('FOO', state='pulled', pulled_at=TODAY, pulled_by=BRANCH))
        self.verb('set_status', 'FOO', 'parked', 'Q-002')
        r = self.get('FOO')
        self.assertEqual(r.get('state'), 'parked')
        for key in rmod.PULL_KEYS:
            self.assertFalse(r.has(key), key)

    def test_park_refusals(self):
        self.seed(priced('FOO'), rec('BAR', state='blocked', blocked_on='live-client'),
                  priced('BAZ', **PARKED))
        for args, exc in ((('FOO', 'parked', None), Usage),
                          (('FOO', 'parked', 'q28'), Usage),
                          (('FOO', 'open', 'Q-001'), Usage),
                          (('BAR', 'parked', 'Q-001'), Refusal),
                          (('BAZ', 'parked', 'Q-003'), Refusal)):
            with self.assertRaises(exc, msg=args):
                self.verb('set_status', *args)

    def test_set_neither_parks_nor_unparks_nor_writes_their_keys(self):
        self.seed(priced('FOO'), priced('BAR', **PARKED))
        for rid, changes in (('FOO', [('state', ['parked'])]), ('BAR', [('state', ['open'])]),
                             ('FOO', [('q', ['Q-001'])]), ('FOO', [('parked_at', [TODAY])]),
                             ('FOO', [('resume', ['R-9001'])])):
            with self.assertRaises(Refusal, msg=(rid, changes)):
                self.verb('set', rid, changes)


class Unpark(StoreCase):
    remote = False

    def test_unpark_opens_the_record_and_sets_resume(self):
        self.seed(priced('FOO', **PARKED))
        self.verb('unpark', 'FOO', 'R-9001')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('resume')), ('open', 'R-9001'))
        self.assertFalse(r.has('q'))
        self.assertFalse(r.has('parked_at'))
        self.assertEqual(omod.classify(r)[0], omod.PULLABLE)

    def test_unpark_refusals(self):
        self.seed(priced('FOO', **PARKED), priced('BAR'))
        for args, exc in ((('BAR', 'R-9001'), Refusal),      # not parked
                          (('FOO', None), Usage),            # no ruling
                          (('FOO', 'R1'), Usage)):           # not a ruling id
            with self.assertRaises(exc, msg=args):
                self.verb('unpark', *args)
        self.assertEqual(self.get('FOO').get('state'), 'parked')

    def test_any_well_shaped_ruling_unparks(self):
        self.seed(priced('FOO', **PARKED))
        self.verb('unpark', 'FOO', 'R-9999')
        self.assertEqual(self.get('FOO').get('resume'), 'R-9999')


class ParkedRankAndCap(unittest.TestCase):

    def test_resume_ranks_above_priority_and_below_a_pin(self):
        s = snap(priced('AAA', priority='1'), priced('BBB', priority='5', resume='R-9001'),
                 priced('CCC', priority='5', pin='1'), priced('DDD', priority='4', resume='R-9002'))
        ranks = omod.ranked(s)
        self.assertEqual(sorted(ranks, key=ranks.get), ['CCC', 'DDD', 'BBB', 'AAA'])

    def test_a_parked_record_takes_no_place_in_the_cap(self):
        s = snap(priced('AAA', priority='1', **PARKED), priced('BBB', priority='1', **PARKED),
                 priced('CCC', priority='3'), priced('DDD', priority='4'),
                 priced('EEE', priority='5'))
        ids, passed = offer_ids(s, cap=3)
        self.assertEqual(ids, ['CCC', 'DDD', 'EEE'])
        self.assertNotIn('AAA', passed)

    def test_a_parked_record_is_not_in_flight(self):
        s = snap(priced('AAA', **PARKED))
        self.assertEqual(omod.classify(s.get('AAA'))[0], omod.PARKED)


class ParkedFields(unittest.TestCase):

    def test_the_parked_keys_validate(self):
        self.assertEqual(messages(state='parked', q='Q-001', parked_at='2026-10-01'), [])
        self.assertEqual(messages(resume='R-9001'), [])
        self.assertTrue(messages(state='parked', q='28'))
        self.assertTrue(messages(state='parked', parked_at='yesterday'))
        self.assertTrue(messages(resume='Q-001'))

    def test_parked_age_in_days(self):
        r = rmod.parse('backlog/FOO.md', priced('FOO', **PARKED)['FOO'], 'FOO')
        self.assertEqual(rmod.parked_days(r, TODAY), 6)
        bar = rmod.parse('backlog/BAR.md', priced('BAR')['BAR'], 'BAR')
        self.assertIsNone(rmod.parked_days(bar, TODAY))


if __name__ == '__main__':
    unittest.main()
