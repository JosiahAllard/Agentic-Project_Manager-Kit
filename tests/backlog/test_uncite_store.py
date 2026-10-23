"""`uncite` (docs/backlog.md "Uncite"): every owner ruling or question id a record's body cites is
rewritten into plain words, and the header keys that hold only one (`priority_ruling`,
`hold_ruling`) are dropped, in one store commit; a line no rule words cleanly is left as it was and
listed for a hand edit, in the dry run and the apply output alike. And the clear path for the keys no verb writes: `set ID --unset K`."""
import unittest

from kitlib.backlog import opener
from kitlib.backlog import plan as pmod
from kitlib.backlog import store as smod
from kitlib.backlog.errors import Refusal, Usage

from tests import inproc
from tests.backlog.helpers import StoreCase, rec

BACKLOG = inproc.load_tool('backlog')


class Line(unittest.TestCase):
    """uncite_line: each rule, and the lines no rule words cleanly, which it leaves as they were
    for a hand edit. No rewrite says `the owner` or `the owner's`."""

    def check(self, before, after, opens=True):
        self.assertEqual(pmod.uncite_line(before, opens), (after, False), before)
        self.assertNotRegex(after, r'\b(?:The|the) owner\b(?!_)', before)
        # idempotent: the rewrite cites nothing, so a second pass leaves it
        self.assertEqual(pmod.uncite_line(after, opens), (after, False), after)

    def left(self, line, opens=True):
        """The line comes back unchanged and listed for a hand edit."""
        self.assertEqual(pmod.uncite_line(line, opens), (line, True), line)

    def test_each_rule_reads_as_words(self):
        for before, after in (
                ('Per R-9033, x', 'Per an owner ruling, x'),
                ('Waiting on Q-123.', 'Waiting on an owner question.'),
                ('Asked in Q-301.', 'Asked in an owner question.'),
                ('x (R-9033).', 'x.'),
                ('x (see R-9033 and R-9034).', 'x.'),
                ('- R-9033: the hub', '- The hub'),
                ('- Note: R-9033, keep it', '- Note: keep it'),
                ('(R-9033: a split) it stays', '(a split) it stays'),
                ('it stays (a split; R-9033)', 'it stays (a split)'),
                ('The ruling R-9033 says', 'The ruling says'),
                ("Its owner's ruling PM R-9033 holds", "Its owner's ruling holds"),
                ('answered by R-9033.', 'answered by an owner ruling.'),
                ('Settled, R-9033.', 'Settled.'),
                ('under R-9033 ruling the hub moves', 'under an owner ruling the hub moves'),
                ('would break PM R-9144)', 'would break an owner ruling)'),
                ('See R-9033.', ''),
                ('- See R-9001.', ''),
                ('(R-9001)', '')):
            self.check(before, after)

    def test_a_key_or_lead_word_goes_with_its_cite(self):
        for before, after in (
                ('Was: area fleet, value 1 (owner), effort -, value_ruling R-9189.',
                 'Was: area fleet, value 1 (owner), effort -.'),
                ('ref: EX-STATE11; priority_per: R-9499', 'ref: EX-STATE11'),
                ('Found by the counter analysis (owner R-9264; research store, logs)',
                 'Found by the counter analysis (research store, logs)'),
                ('merge once the corrections question is ruled (PM R-9843)',
                 'merge once the corrections question is ruled')):
            self.check(before, after)

    def test_a_parenthesis_of_cites_with_a_part_goes(self):
        for before, after in (
                ('budget (PM R-9270; PM R-9246, PM R-9264 O2). The only figures',
                 'budget. The only figures'),
                ('a run (B2; PM R-9289 Q2)', 'a run (B2)'),
                ('a census (R-9618 b) logs it', 'a census logs it'),
                ('it refuses (R-9618(c)) and logs', 'it refuses and logs')):
            self.check(before, after)

    def test_a_dated_cite_reads_as_words(self):
        self.check('PM R-9482 (2026-10-07): the nightly',
                   'An owner ruling (2026-10-07): the nightly')

    def test_a_cite_at_the_start_of_a_continued_line_is_left(self):
        # the line continues the sentence before it: no rule knows the word before the cite
        self.left('R-9482 (2026-10-07): the nightly', opens=False)
        self.check('- R-9482 (2026-10-07): x', '- An owner ruling (2026-10-07): x', opens=False)

    def test_a_noun_with_no_article_keeps_its_cite_for_a_hand_edit(self):
        # dropping the cite would leave `Owner ruling wants`, `owner question.`
        self.left('Owner ruling PM R-9515 wants the split')
        self.left('Owner ruling R-9033 says')
        self.left('the hub moves; owner question Q-301.')

    def test_a_run_of_cites_after_a_comma_keeps_its_plural(self):
        # the comma joins the run: never a tag, so two rulings never read as one
        self.check('Superseded by R-9001, R-9002.', 'Superseded by owner rulings.')
        self.check('per R-9001, R-9002, R-9003, more', 'per owner rulings, more')
        self.left('the plan R-9001, R-9002.')

    def test_a_subject_or_possessive_cite_is_left(self):
        # `an owner ruling` there says some ruling, where the line meant one
        for line in ('PM R-9144 does not settle this case.',
                     'An interrupt ends it. PM R-9144 does not settle this case.',
                     'R-9033 says x', 'done. R-9033 says x', '(R-9033 added delete)',
                     'R-9033 ruling holds', "PM R-9104's approved build order",
                     "R-9116's split holds."):
            self.left(line)

    def test_a_question_cite_for_its_answer_is_left(self):
        for line in ('Done per Q-301.', 'Answered by Q-301.', 'Moved after Q-301 and x'):
            self.left(line)

    def test_a_cite_list_that_leaves_a_joining_word_first_is_left(self):
        self.left('(PM R-9526 and R-9746, and the HKCU rule, PM R-9595)')
        self.left('- R-9033, and then x')
        self.left('(R-9033, which sets the cap) it holds')
        self.check('- Q-301: which one?', '- Which one?')

    def test_a_cite_that_is_a_word_no_rule_places_is_left(self):
        for line in (
                # a verb before it, or a verb lost after it
                'Accept fallback ruled PM R-9129 (blind Accept after seeding); BUILT on',
                '- A step in SKILL (PM R-9953, which supersedes R-9948 via R-9952; PM R-9954).',
                '## Steps (PM R-9617 approved the API set; PM R-9618 the details; '
                'PM R-9950, R-9951 the B3 answers)',
                "Supersedes R-9116's plan",
                # a subject or a possessive the old fallback made `the owner`
                'The owner_ask cite of R-9775 is stale (R-9775 is the LOCKER plan); R-9164 does '
                'not state it',
                'The R-9033 wording holds.',
                '(R-9250 kit backlog)',
                'the drain and R-9775 are done',
                'One R-9508 debug run',
                '(checked R-9525; R-9524 holds only tools)',
                'the next PM R-9296 rebuild',
                'the risk (PM R-9704 phase 5a)',
                # a run a subject rule cannot word as one, a mixed run, a cut-off parenthesis
                'R-9033 and R-9034 then x',
                'ref: PM R-9185; was priority 4 (owner, R-9265).',
                'x (R-9950;'):
            self.left(line)

    def test_a_cite_glued_by_a_hyphen_is_left(self):
        self.left('note: reverted to its pre-R-9102 priority')
        self.left('note: reverted to its pre-R-9102 priority (PM R-9106)')

    def test_a_leading_mark_is_the_bodys_to_join(self):
        self.assertEqual(pmod.uncite_line('(R-9033), then x'), (', then x', False))
        self.assertEqual(pmod.uncite_line('  (PM R-9818). It checks U1'),
                         ('  . It checks U1', False))

    def test_what_is_not_an_owner_cite_is_untouched(self):
        for line in ('T-1234 and item:FOO and R-12 stay.', 'XR-9033 and R-9033x-1', '',
                     'Blocked on owner (2026-01-01): per R-9033'):
            self.assertEqual(pmod.uncite_line(line), (line, False), line)


class Body(unittest.TestCase):
    """_uncite_body: the stray mark a rewrite leaves at a line's start, and hand-edit lines."""

    def test_a_stray_mark_rejoins_the_line_above(self):
        body, rewritten, hand = pmod._uncite_body(
            "the cap\n(PM R-9922), not this record's.\n- fallback (PM R-9823). No RSProx\n"
            '  (PM R-9818). It checks U1, the\n  more')
        self.assertEqual(body.split('\n'), ['the cap,', "not this record's.",
                                            '- fallback. No RSProx.', '  It checks U1, the',
                                            '  more'])
        self.assertEqual(hand, [])
        self.assertIn(['the cap', 'the cap,'], rewritten)

    def test_a_stray_mark_after_a_finished_line_is_dropped(self):
        body, _, hand = pmod._uncite_body('Done.\n(R-9001). it checks U1\n\n(R-9002), more')
        self.assertEqual(body.split('\n'), ['Done.', 'It checks U1', '', 'More'])
        self.assertEqual(hand, [])
        body, _, _ = pmod._uncite_body('## Heading\n(R-9001), more')
        self.assertEqual(body.split('\n'), ['## Heading', 'More'])

    def test_a_line_of_only_cites_goes(self):
        body, rewritten, _ = pmod._uncite_body('x\n(R-9001)\nSee R-9002.\ny')
        self.assertEqual(body.split('\n'), ['x', 'y'])
        self.assertEqual(rewritten, [['(R-9001)', None], ['See R-9002.', None]])

    def test_a_hand_edit_line_is_kept_and_numbered(self):
        body, rewritten, hand = pmod._uncite_body(
            'x\n(R-9001)\nOne R-9508 debug run\n(R-9002). It checks')
        self.assertEqual(body.split('\n'), ['x', 'One R-9508 debug run', '(R-9002). It checks'])
        # the line after one left for a hand edit cannot give it its mark: left too
        self.assertEqual(hand, [(2, 'One R-9508 debug run'), (3, '(R-9002). It checks')])
        self.assertEqual(rewritten, [['(R-9001)', None]])


BODY_A = 'Per R-9033, the hub moves.\n\nSee R-9034.\nBlocked on owner (2026-01-01): R-9035 says wait'
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
                         ['Per an owner ruling, the hub moves.', '',
                          'Blocked on owner (2026-01-01): R-9035 says wait'])
        self.assertEqual(b.body.strip().split('\n'), ['- Which one?', '- T-1234 stays'])
        # what uncite keeps, byte for byte
        for key in ('summary', 'ref', 'owner_ask', 'q', 'parked_at', 'resume', 'priority',
                    'priority_by', 'state', 'scope'):
            for rid in ('AAA', 'BBB'):
                self.assertEqual(self.get(rid).all(key), before.get(rid).all(key), (rid, key))
        self.assertEqual(self.get('CCC').text, before.get('CCC').text)

    def test_the_output_counts_and_lists_each_change(self):
        self.seed_three()
        out = '\n'.join(self.verb('uncite').lines)
        self.assertIn('UNCITE: 3 cite(s) in 2 record(s): 3 body line(s) rewritten, '
                      '2 header line(s) dropped, 0 line(s) left for a hand edit', out)
        self.assertIn('    - Per R-9033, the hub moves.', out)
        self.assertIn('    + Per an owner ruling, the hub moves.', out)
        self.assertIn('    - See R-9034.\n    + (line removed)', out)
        self.assertIn('    dropped priority_ruling: R-9001', out)
        self.assertIn('    dropped hold_ruling: R-9002', out)
        self.assertNotIn('NEEDS HAND EDIT', out)

    def test_a_line_no_rule_words_is_left_and_listed_for_a_hand_edit(self):
        self.seed(rec('AAA', body='Per R-9033, the hub moves.\nOne R-9508 debug run first.'),
                  rec('BBB', body='the drain and R-9775 are done'))
        dry = opener.uncite_dry(self.ctx)
        self.assertEqual(sorted(dry.writes), ['AAA'])
        change = self.verb('uncite')
        self.assertEqual(change.lines, dry.lines)
        where = self.store.load().where
        a, b = self.get('AAA'), self.get('BBB')
        self.assertEqual(a.body.strip().split('\n'),
                         ['Per an owner ruling, the hub moves.', 'One R-9508 debug run first.'])
        self.assertEqual(b.body.strip(), 'the drain and R-9775 are done')
        self.assertIn('2 line(s) left for a hand edit', change.lines[0])
        # listed in the dry run and the apply output alike, by store, record and file line
        tail = change.lines[change.lines.index(next(
            l for l in change.lines if l.startswith('NEEDS HAND EDIT (2 line(s)'))) + 1:]
        lines_a, lines_b = a.text.split('\n'), b.text.split('\n')
        n_a = lines_a.index('One R-9508 debug run first.') + 1
        n_b = lines_b.index('the drain and R-9775 are done') + 1
        self.assertEqual(tail, ['  %s AAA line %d: One R-9508 debug run first.' % (where, n_a),
                                '  %s BBB line %d: the drain and R-9775 are done' % (where, n_b)])
        # a second run writes nothing and still lists them
        tip = self.remote_tip()
        again = self.verb('uncite')
        self.assertEqual(again.writes, {})
        self.assertEqual(self.remote_tip(), tip)
        self.assertIn('nothing to rewrite', again.lines[0])
        self.assertIn('2 line(s) still cite one', again.lines[0])
        self.assertEqual(again.lines[-2:], tail)

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
