"""The owner's answer filed into a record (docs/backlog.md "Records"): the protected `answer:
<Q-nnn> <at>` key, one line per question, that only kitlib/backlog/plan.py plan_answer writes,
with its body section; the wait ends on the last answer (unpark, the owner blocker cleared); a
free-text Other answer leaves the review mark; a repeat changes nothing."""
import unittest

from kitlib.backlog import offer as omod
from kitlib.backlog import record as rmod
from kitlib.backlog.errors import NotFound, Refusal, Usage

from tests.backlog import test_verbs
from tests.backlog.helpers import StoreCase, messages, priced, rec

AT = '2026-10-24T04:40:00-07:00'
AT2 = '2026-10-25T09:00:00-07:00'
ANSWER = dict(title='Keep the cap?', label='Yes, keep it')


def parked(rid, q='Q-101', **over):
    over.setdefault('parked_at', '2026-10-01')
    if q is not None:
        over['q'] = q
    return priced(rid, state='parked', **over)


def held(rid, ask='Q-101: keep the cap?', on='owner', **over):
    return priced(rid, state='blocked', blocked_on=on, owner_ask=ask, **over)


class Field(unittest.TestCase):

    def test_a_well_formed_answer_passes_check(self):
        self.assertEqual(messages(answer='Q-101 %s' % AT), [])
        self.assertEqual(messages(answer=['Q-101 %s' % AT, 'Q-102 %s' % AT2]), [])

    def test_a_malformed_answer_fails_check(self):
        for bad in ('Q-101', 'Q-1 %s' % AT, 'R-1234 %s' % AT, 'Q-101 2026-10-24',
                    'Q-101 2026-10-24T04:40:00', 'Q-101 %s x' % AT, 'Q-101  %s' % AT,
                    'Q-101 yesterday'):
            self.assertTrue(messages(answer=bad), bad)
            self.assertIsNotNone(rmod._answer(bad), bad)

    def test_one_line_per_question(self):
        got = messages(answer=['Q-101 %s' % AT, 'Q-101 %s' % AT2])
        self.assertTrue(any('one line per question' in m for m in got), got)


class AnswerCase(StoreCase):
    remote = False

    def answer(self, rid, qid='Q-101', at=AT, **kw):
        for k, v in ANSWER.items():
            kw.setdefault(k, v)
        kw.setdefault('role_checked', True)
        return self.verb('answer', rid, qid, at, **kw)


class Set(AnswerCase):

    def test_set_cannot_write_or_unset_the_answer(self):
        self.seed(rec('FOO', answer='Q-101 %s' % AT), rec('BAR'))
        with self.assertRaises(Refusal) as cm:
            self.verb('set', 'BAR', [('answer', ['Q-101 %s' % AT])])
        self.assertIn('plan_answer', str(cm.exception))
        with self.assertRaises(Refusal):
            self.verb('set', 'FOO', [], unset=('answer',))
        self.assertEqual(self.get('FOO').all('answer'), ['Q-101 %s' % AT])
        self.assertFalse(self.get('BAR').has('answer'))


class States(AnswerCase):

    def test_parked_on_this_question_unparks_with_the_line_and_section(self):
        self.seed(parked('FOO', body='Old body.'))
        change = self.answer('FOO', note='keep it at 4', via='local')
        r = self.get('FOO')
        self.assertEqual(r.get('state'), 'open')
        self.assertFalse(r.has('q') or r.has('parked_at'))
        self.assertEqual(r.all('answer'), ['Q-101 %s' % AT])
        self.assertTrue(r.body.startswith('Old body.\n\n## Owner answer (Action Hub, %s)' % AT))
        for want in ('Question: Q-101, Keep the cap?', 'Answer: Yes, keep it',
                     'Note: keep it at 4', 'Via: local'):
            self.assertIn(want, r.body)
        self.assertEqual(change.subject, 'backlog: owner answer FOO on Q-101')

    def test_parked_on_another_question_stays_parked(self):
        self.seed(parked('FOO', q='Q-102'))
        change = self.answer('FOO')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('q')), ('parked', 'Q-102'))
        self.assertEqual(r.all('answer'), ['Q-101 %s' % AT])
        self.assertIn('still waits on Q-102', '\n'.join(change.lines))

    def test_a_question_the_wait_does_not_name_leaves_the_state(self):
        self.seed(held('FOO', ask='Q-102: which way?'), parked('BAR', q=None))
        self.answer('FOO')
        self.answer('BAR')
        foo, bar = self.get('FOO'), self.get('BAR')
        self.assertEqual((foo.get('state'), foo.all('blocked_on')), ('blocked', ['owner']))
        self.assertEqual(foo.get('owner_ask'), 'Q-102: which way?')
        self.assertEqual(bar.get('state'), 'parked')
        self.assertEqual(foo.all('answer'), ['Q-101 %s' % AT])
        self.assertEqual(bar.all('answer'), ['Q-101 %s' % AT])

    def test_waiting_questions_keep_it_parked(self):
        self.seed(parked('FOO'))
        self.answer('FOO', waiting=('Q-102',))
        self.assertEqual(self.get('FOO').get('state'), 'parked')

    def test_two_questions_unpark_only_on_the_last_either_order(self):
        for first, last in (('Q-101', 'Q-102'), ('Q-102', 'Q-101')):
            rid = 'F' + first[-1]
            self.seed(parked(rid))
            self.answer(rid, first, waiting=(last,))
            self.assertEqual(self.get(rid).get('state'), 'parked', first)
            self.answer(rid, last, at=AT2)
            r = self.get(rid)
            self.assertEqual(r.get('state'), 'open', first)
            self.assertEqual(sorted(r.all('answer')), sorted(['%s %s' % (first, AT),
                                                              '%s %s' % (last, AT2)]))
            self.assertEqual(r.body.count('## Owner answer'), 2)

    def test_the_owner_blocker_alone_opens_the_record(self):
        self.seed(held('FOO'))
        self.answer('FOO')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('blocked_on')), ('open', ['-']))
        self.assertFalse(r.has('owner_ask'))
        self.assertEqual(r.all('answer'), ['Q-101 %s' % AT])

    def test_other_blockers_stay(self):
        self.seed(held('FOO', on=['owner', 'item:BAR']), rec('BAR'))
        self.answer('FOO')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.all('blocked_on')), ('blocked', ['item:BAR']))
        self.assertFalse(r.has('owner_ask'))

    def test_a_minus_one_hold_keeps_its_owner_ask(self):
        self.seed(held('FOO', priority='-1', priority_by='-'))
        self.answer('FOO')
        r = self.get('FOO')
        self.assertEqual((r.get('state'), r.get('priority')), ('open', '-1'))
        self.assertEqual(r.get('owner_ask'), 'Q-101: keep the cap?')

    def test_clearing_the_owner_needs_the_role_check(self):
        self.seed(held('FOO'))
        with self.assertRaises(Refusal) as cm:
            self.answer('FOO', role_checked=False)
        self.assertIn('unblock-owner', str(cm.exception))
        r = self.get('FOO')
        self.assertEqual(r.get('state'), 'blocked')
        self.assertFalse(r.has('answer'))

    def test_pulled_and_open_records_get_the_answer_only(self):
        self.seed(priced('FOO', state='pulled', pulled_at='2026-10-20',
                         pulled_by='tools/x'), priced('BAR'))
        self.answer('FOO')
        self.answer('BAR')
        foo, bar = self.get('FOO'), self.get('BAR')
        self.assertEqual((foo.get('state'), foo.get('pulled_by')), ('pulled', 'tools/x'))
        self.assertEqual(bar.get('state'), 'open')
        self.assertEqual(foo.all('answer'), ['Q-101 %s' % AT])
        self.assertEqual(bar.all('answer'), ['Q-101 %s' % AT])

    def test_a_missing_record_is_not_found(self):
        with self.assertRaises(NotFound):
            self.answer('NOPE')


class Other(AnswerCase):

    def test_an_other_answer_unparks_but_is_marked_to_review(self):
        self.seed(parked('FOO'))
        self.answer('FOO', label='Other', note='do it differently', other=True)
        r = self.get('FOO')
        self.assertEqual(r.get('state'), 'open')
        self.assertEqual(r.get('review'), 'owner-answer %s' % self.ctx.date())
        self.assertEqual(omod.classify(r)[0], omod.REVIEW)
        self.assertIn('Note: do it differently', r.body)
        with self.assertRaises(Refusal) as cm:
            self.verb('pull', 'FOO')
        self.assertIn('marked for review', str(cm.exception))

    def test_a_chosen_answer_writes_no_mark(self):
        self.seed(parked('FOO'))
        self.answer('FOO')
        self.assertFalse(self.get('FOO').has('review'))


class Repeat(AnswerCase):

    def test_the_same_answer_again_changes_nothing(self):
        self.seed(parked('FOO'))
        self.answer('FOO')
        text = self.get('FOO').text
        change = self.answer('FOO', other=True)
        self.assertEqual(change.writes, {})
        self.assertEqual(self.get('FOO').text, text)

    def test_a_re_answer_replaces_the_line_and_appends(self):
        self.seed(parked('FOO'))
        self.answer('FOO')
        self.answer('FOO', at=AT2, label='No, drop it')
        r = self.get('FOO')
        self.assertEqual(r.all('answer'), ['Q-101 %s' % AT2])
        self.assertEqual(r.body.count('## Owner answer'), 2)
        self.assertIn('Answer: No, drop it', r.body)


class Refusals(AnswerCase):

    def test_bad_arguments_are_usage_and_write_nothing(self):
        self.seed(parked('FOO'))
        for kw in (dict(qid='R-1234'), dict(at='2026-10-24'), dict(label=' '),
                   dict(waiting=('soon',))):
            with self.assertRaises(Usage, msg=kw):
                self.answer('FOO', **kw)
        self.assertFalse(self.get('FOO').has('answer'))

    def test_a_machine_path_in_the_note_is_refused(self):
        self.seed(parked('FOO'))
        for drive in test_verbs.MachinePaths.DRIVES:
            with self.assertRaises(Usage) as cm:
                self.answer('FOO', note='the notes are in %s now' % drive)
            self.assertIn('machine path', str(cm.exception))
        self.assertEqual(self.get('FOO').get('state'), 'parked')


if __name__ == '__main__':
    unittest.main()
