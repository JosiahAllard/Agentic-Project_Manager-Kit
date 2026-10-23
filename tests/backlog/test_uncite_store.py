"""`uncite` (docs/backlog.md "Uncite"): every owner ruling or question id a record's body cites is
rewritten into plain words, and the header keys that hold only one (`priority_ruling`,
`hold_ruling`) are dropped, in one store commit; a dry run lists what it would write and the lines
whose wording wants a reader. And the clear path for the keys no verb writes: `set ID --unset K`."""
import unittest

from kitlib.backlog import opener
from kitlib.backlog import plan as pmod
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Refusal, Usage

from tests import inproc
from tests.backlog.helpers import StoreCase, rec

BACKLOG = inproc.load_tool('backlog')


class Line(unittest.TestCase):
    """uncite_line: each rule, the fallback and its wording flag."""

    def check(self, before, after, flagged=False, opens=True):
        self.assertEqual(pmod.uncite_line(before, opens), (after, flagged), before)
        # idempotent: the rewrite cites nothing, so a second pass leaves it
        self.assertEqual(pmod.uncite_line(after, opens), (after, False), after)

    def test_each_rule_reads_as_words(self):
        for before, after in (
                ('Per R-2033, x', "Per the owner's ruling, x"),
                ('Done per Q-301.', "Done per the owner's answer."),
                ('x (R-2033).', 'x.'),
                ('x (see R-2033 and R-2034).', 'x.'),
                ('(R-2033 added delete)', '(the owner added delete)'),
                ('- R-2033: the hub', '- The hub'),
                ('- Note: R-2033, keep it', '- Note: keep it'),
                ('(R-2033: a split) it stays', '(a split) it stays'),
                ('it stays (a split; R-2033)', 'it stays (a split)'),
                ('R-2033 says x', 'The owner says x'),
                ('Owner ruling R-2033 says', 'Owner ruling says'),
                ('answered by R-2033.', 'answered by the owner.'),
                ('Settled, R-2033.', 'Settled.'),
                ('The R-2033 wording holds.', "The owner's wording holds."),
                ('under R-2033 ruling the hub moves', "under the owner's ruling the hub moves")):
            self.check(before, after)

    def test_a_cite_mid_sentence_on_a_continued_line_is_not_capitalised(self):
        self.check('R-2033 says x', 'the owner says x', opens=False)
        self.check('done. R-2033 says x', 'done. The owner says x', opens=False)

    def test_a_cite_no_rule_places_falls_back_and_is_flagged(self):
        self.check('See R-2033.', 'See an owner ruling.', flagged=True)
        self.check('Waiting on Q-123.', 'Waiting on an owner question.', flagged=True)
        self.check('R-2033 and R-2034 then x', 'An owner ruling then x', flagged=True)

    def test_odd_punctuation_left_behind_is_flagged(self):
        self.check('(R-2033), then x', ', then x', flagged=True)

    def test_what_is_not_an_owner_cite_is_untouched(self):
        for line in ('T-1234 and item:FOO and R-123 stay.', 'XR-2033 and R-2033x-1', '',
                     'Blocked on owner (2026-01-01): per R-2033'):
            self.assertEqual(pmod.uncite_line(line), (line, False), line)


BODY_A = 'Per R-2033, the hub moves.\n\nSee R-2034.\nBlocked on owner (2026-01-01): R-2035 says wait'
BODY_B = '- Q-301: which one?\n- T-1234 stays'


class Store(StoreCase):
    remote = False

    def seed_three(self):
        self.seed(rec('AAA', priority='2', priority_by='owner', priority_ruling='R-9001',
                      body=BODY_A),
                  rec('BBB', priority='-1', priority_by='-', owner_ask='which one?',
                      hold_ruling='R-9002', q='Q-301', parked_at='2026-10-01', state='parked',
                      resume='R-9003', ref='R-9004 notes', body=BODY_B),
                  rec('CCC', body='Nothing cited here; T-1234 is a task.'))

    def test_one_commit_rewrites_the_bodies_and_drops_the_ruling_headers(self):
        self.seed_three()
        before = self.store.load()
        t0 = self.remote_tip()
        change = self.verb('uncite')
        t1 = self.remote_tip()
        self.assertEqual(self.repo.git('rev-list', '--count', '%s..%s' % (t0, t1)).strip(), '1')
        self.assertEqual(sorted(change.writes), ['AAA', 'BBB'])
        self.assertIsNone(change.ruling)
        self.assertEqual(change.subject, 'backlog: uncite 3 cite(s) in 2 record(s)')
        message = self.repo.git('log', '-1', '--format=%B', smod.STORE_REF)
        self.assertNotIn('R-', message)
        a, b = self.get('AAA'), self.get('BBB')
        self.assertIsNone(a.get('priority_ruling'))
        self.assertIsNone(b.get('hold_ruling'))
        self.assertEqual(a.body.strip().split('\n'),
                         ["Per the owner's ruling, the hub moves.", '', 'See an owner ruling.',
                          'Blocked on owner (2026-01-01): R-2035 says wait'])
        self.assertEqual(b.body.strip().split('\n'), ['- Which one?', '- T-1234 stays'])
        # what uncite keeps, byte for byte
        for key in ('summary', 'ref', 'owner_ask', 'q', 'parked_at', 'resume', 'priority',
                    'priority_by', 'state', 'scope'):
            for rid in ('AAA', 'BBB'):
                self.assertEqual(self.get(rid).all(key), before.get(rid).all(key), (rid, key))
        self.assertEqual(self.get('CCC').text, before.get('CCC').text)

    def test_the_output_counts_and_lists_the_wording_to_check(self):
        self.seed_three()
        out = '\n'.join(self.verb('uncite').lines)
        self.assertIn('UNCITE: 3 cite(s) in 2 record(s): 3 body line(s) rewritten, '
                      '2 header line(s) dropped', out)
        self.assertIn('    - Per R-2033, the hub moves.', out)
        self.assertIn("    + Per the owner's ruling, the hub moves.", out)
        self.assertIn('    dropped priority_ruling: R-9001', out)
        self.assertIn('    dropped hold_ruling: R-9002', out)
        self.assertIn('CHECK WORDING (1 line(s)', out)
        self.assertIn('  AAA: See an owner ruling.', out)

    def test_a_second_run_writes_nothing(self):
        self.seed_three()
        self.verb('uncite')
        tip = self.remote_tip()
        change = self.verb('uncite')
        self.assertEqual(change.writes, {})
        self.assertIn('nothing to rewrite', change.lines[0])
        self.assertEqual(self.remote_tip(), tip)

    def test_the_dry_run_publishes_nothing(self):
        self.seed_three()
        tip = self.remote_tip()
        change = opener.uncite_dry(self.ctx)
        self.assertEqual(sorted(change.writes), ['AAA', 'BBB'])
        self.assertEqual(self.remote_tip(), tip)
        self.assertEqual(self.get('AAA').get('priority_ruling'), 'R-9001')

    def test_the_planner_plans_uncite(self):
        self.seed_three()
        args = BACKLOG.build_parser().parse_args(['uncite', '--apply'])
        plan = opener.planner(args, self.ctx)
        self.assertEqual(sorted(plan(self.store.load()).writes), ['AAA', 'BBB'])


class ClearPath(StoreCase):
    """`set ID --unset K` clears a key no verb writes (plan.UNWRITTEN); writing one stays refused.
    Clearing `resume` is the owner's and the PM's (`set-price-owner`)."""
    remote = False

    def setUp(self):
        super().setUp()
        self.seed(rec('FOO', hold_ruling='R-9002', resume='R-9003', priority='3',
                      priority_by='agent', priority_ruling='R-9001'))

    def test_each_is_cleared_once_the_role_check_ran(self):
        for key in pmod.UNWRITTEN:
            change = self.verb('set', 'FOO', [], (key,), role_checked=True)
            self.assertIsNone(self.get('FOO').get(key), key)
            self.assertIn('-> (absent)', '\n'.join(change.lines), key)

    def test_the_ruling_keys_clear_without_the_role_check(self):
        for key in ('hold_ruling', 'priority_ruling'):
            self.verb('set', 'FOO', [], (key,))
            self.assertIsNone(self.get('FOO').get(key), key)

    def test_clearing_resume_without_the_role_check_is_refused(self):
        with self.assertRaises(Refusal) as cm:
            self.verb('set', 'FOO', [], ('resume',))
        self.assertNotIsInstance(cm.exception, Usage)
        self.assertIn('`set-price-owner`', str(cm.exception))
        self.assertEqual(self.get('FOO').get('resume'), 'R-9003')

    def test_writing_one_is_still_refused(self):
        for key in pmod.UNWRITTEN:
            with self.assertRaises(Refusal) as cm:
                self.verb('set', 'FOO', [(key, ['R-9009'])], role_checked=True)
            self.assertIn('is no longer written', str(cm.exception))
            self.assertIn('`set FOO --unset %s` clears it' % key, str(cm.exception))


class Cli(unittest.TestCase):
    def test_uncite_parses_with_and_without_apply(self):
        parser = BACKLOG.build_parser()
        self.assertFalse(parser.parse_args(['uncite']).apply)
        self.assertTrue(parser.parse_args(['uncite', '--apply']).apply)

    def test_set_status_takes_an_optional_q(self):
        parser = BACKLOG.build_parser()
        self.assertIsNone(parser.parse_args(['set-status', 'FOO', 'parked']).q)
        self.assertEqual(parser.parse_args(['set-status', 'FOO', 'parked', '--q', 'Q-001']).q,
                         'Q-001')


if __name__ == '__main__':
    unittest.main()
