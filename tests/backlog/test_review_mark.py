"""The review mark (`review: <source> <YYYY-MM-DD>`, docs/backlog.md "Records"): only the
library's `add(review=)` writes it (the Action Hub's add; the CLI has no flag), `pull` refuses a
marked record, `next` never offers it and the banner counts it as `to review`, and a
coordinator's `clear-review ID --reason T` drops it (kitlib/backlog/plan.py plan_add,
plan_pull, plan_clear_review; offer.py classify, not_offered_why)."""
import unittest

from kitlib.backlog import offer as omod
from kitlib.backlog import record as rmod
from kitlib.backlog.errors import NotFound, Refusal, Usage

from tests.backlog.helpers import ADD_NONE, StoreCase, messages, priced, rec

MARK = 'owner-hub 2026-10-15'


class ReviewCase(StoreCase):
    remote = False

    def offered(self):
        snap = self.store.load()
        (offers, passed), refusals, _ranks = omod.next_offer(snap, [], cap=None)
        self.assertEqual(refusals, [])
        return [o.rec.id for o in offers], passed


class Field(unittest.TestCase):

    def test_a_well_formed_mark_passes_check(self):
        self.assertEqual(messages(review=MARK), [])

    def test_a_malformed_mark_fails_check(self):
        for bad in ('owner-hub', 'Owner 2026-10-15', 'owner-hub 15-10-2026', 'owner-hub 2026-10-15 x',
                    'owner-hub  2026-10-15'):
            self.assertTrue(messages(review=bad), bad)
        self.assertIsNone(rmod._review(MARK))

    def test_the_mark_is_single_valued(self):
        self.assertTrue(messages(review=[MARK, 'pm 2026-10-14']))


class Add(ReviewCase):

    def test_add_with_review_writes_the_mark_and_says_so(self):
        change = self.verb('add', 'NEW', 'a thing the owner added', ['link/src/**'], review=MARK,
                           **ADD_NONE)
        r = self.get('NEW')
        self.assertEqual(r.get('review'), MARK)
        self.assertEqual((r.get('state'), r.get('priority')), ('open', '-'))
        self.assertIn('clear-review NEW', '\n'.join(change.lines))
        self.assertEqual(omod.classify(r)[0], omod.REVIEW)

    def test_add_without_review_writes_no_mark(self):
        self.verb('add', 'NEW', 'a thing an agent found', ['link/src/**'], **ADD_NONE)
        self.assertFalse(self.get('NEW').has('review'))

    def test_a_bad_mark_is_usage_and_writes_nothing(self):
        with self.assertRaises(Usage) as cm:
            self.verb('add', 'NEW', 'a thing the owner added', ['link/src/**'], review='hub',
                      **ADD_NONE)
        self.assertIn('review mark', str(cm.exception))
        self.assertIsNone(self.get('NEW'))

    def test_set_cannot_write_or_unset_the_mark(self):
        self.seed(rec('FOO', review=MARK), rec('BAR'))
        with self.assertRaises(Refusal) as cm:
            self.verb('set', 'BAR', [('review', [MARK])])
        self.assertIn('clear-review', str(cm.exception))
        with self.assertRaises(Refusal):
            self.verb('set', 'FOO', [], unset=('review',))
        self.assertEqual(self.get('FOO').get('review'), MARK)


class PullAndNext(ReviewCase):

    def test_pull_refuses_a_marked_record_priced_or_not(self):
        self.seed(priced('FOO', review=MARK), rec('BAR', review=MARK))
        for rid in ('FOO', 'BAR'):
            with self.assertRaises(Refusal) as cm:
                self.verb('pull', rid)
            self.assertIn('marked for review', str(cm.exception))
            self.assertIn('clear-review %s' % rid, str(cm.exception))
        with self.assertRaises(Refusal):
            self.verb('pull', 'FOO', reason='the owner wants it today')
        self.assertEqual(self.get('FOO').get('state'), 'open')

    def test_next_never_offers_a_marked_record_and_says_why(self):
        self.seed(priced('FOO', priority='1', review=MARK), priced('BAR', priority='4'))
        ids, _passed = self.offered()
        self.assertEqual(ids, ['BAR'])
        snap = self.store.load()
        why = omod.not_offered_why(snap.get('FOO'), {})
        self.assertIn('marked for review (review: %s)' % MARK, why)
        self.assertIn('clear-review FOO', why)

    def test_the_banner_counts_marked_records_only_when_there_are_some(self):
        self.seed(priced('FOO', review=MARK), rec('BAR', review=MARK), priced('BAZ'))
        banner = omod.Banner(self.store.load())
        self.assertEqual([r.id for r in banner.buckets[omod.REVIEW]], ['BAR', 'FOO'])
        self.assertIn('2 to review', banner.lines()[0])
        self.assertIn('1 pullable', banner.lines()[0])
        self.verb('clear_review', 'FOO', 'the coordinator read it')
        self.verb('clear_review', 'BAR', 'the coordinator read it')
        self.assertNotIn('to review', omod.Banner(self.store.load()).lines()[0])

    def test_the_banner_counts_a_marked_record_at_minus_one_as_open(self):
        # A marked record at `-1` is in the `to review` bucket, not `owner to prioritise`, and is
        # still open: the `N at priority -1 (M of them open)` line must not call it not open.
        self.seed(rec('MIN', priority='-1', owner_ask='which?', review=MARK),
                  rec('OWN', priority='-1', owner_ask='which one?'))
        line = omod.Banner(self.store.load()).lines()[0]
        self.assertIn('1 to review', line)
        self.assertIn('1 owner to prioritise', line)
        self.assertNotIn('at priority -1', line)

    def test_a_pulled_minus_one_record_still_shows_the_open_split(self):
        self.seed(rec('MIN', priority='-1', owner_ask='which?', review=MARK),
                  rec('PUL', priority='-1', owner_ask='which one?', state='pulled',
                      pulled_at='2026-10-01', pulled_by='tools/gone'))
        line = omod.Banner(self.store.load()).lines()[0]
        self.assertIn('2 at priority -1 (1 of them open)', line)

    def test_a_marked_record_can_be_priced_and_stays_marked(self):
        self.seed(rec('FOO', review=MARK))
        self.verb('set', 'FOO', [('priority', ['2']), ('priority_by', ['agent'])])
        r = self.get('FOO')
        self.assertEqual((r.get('priority'), r.get('review')), ('2', MARK))
        self.assertEqual(self.offered()[0], [])


class ClearReview(ReviewCase):

    def test_clear_review_drops_the_mark_and_the_record_is_then_offered_and_pullable(self):
        self.seed(priced('FOO', review=MARK))
        change = self.verb('clear_review', 'FOO', 'the coordinator read it and it stands')
        r = self.get('FOO')
        self.assertFalse(r.has('review'))
        self.assertEqual((r.get('priority'), r.get('state')), ('2', 'open'))
        self.assertEqual(change.subject,
                         'backlog: clear review FOO: the coordinator read it and it stands')
        self.assertIn('CLEARED the review mark on FOO (was: %s)' % MARK, change.lines)
        self.assertEqual(self.offered()[0], ['FOO'])
        self.verb('pull', 'FOO')
        self.assertEqual(self.get('FOO').get('state'), 'pulled')

    def test_clear_review_works_on_blocked_and_parked_records(self):
        self.seed(rec('BLK', state='blocked', blocked_on='live-client', review=MARK),
                  rec('PRK', state='parked', q='Q-101', parked_at='2026-10-01', review=MARK))
        for rid, state in (('BLK', 'blocked'), ('PRK', 'parked')):
            self.verb('clear_review', rid, 'the coordinator read it')
            r = self.get(rid)
            self.assertFalse(r.has('review'), rid)
            self.assertEqual(r.get('state'), state)

    def test_clear_review_refusals(self):
        self.seed(rec('FOO'), rec('BAR', review=MARK))
        with self.assertRaises(Refusal) as cm:
            self.verb('clear_review', 'FOO', 'the coordinator read it')
        self.assertIn('no `review:` mark', str(cm.exception))
        with self.assertRaises(Usage):
            self.verb('clear_review', 'BAR', 'ok')
        with self.assertRaises(NotFound):
            self.verb('clear_review', 'NOPE', 'the coordinator read it')
        self.assertEqual(self.get('BAR').get('review'), MARK)


if __name__ == '__main__':
    unittest.main()
