"""kitlib.syncbase's texts and argument rules, no git.

The conflict advice is printed at the moment someone is standing in a half-finished merge and will
do what it says. It used to say "Docs: keyword form, keep both sides' lines", which is wrong for a
row the base deleted on purpose: the union resurrects it. These tests pin the corrected rule.
"""
import contextlib
import io
import os
import sys
import unittest

from kitlib import syncbase

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'tools'))
import sync_base as cli  # noqa: E402

ADVICE = syncbase.conflict_advice('master', '33bfd33cafe', 'Some Model', armed=True)


class ConflictAdviceRefusesTheNaiveRule(unittest.TestCase):
    """The defect this text caused: "keep both sides' lines" as an unqualified instruction."""

    def test_the_old_naive_sentence_is_gone(self):
        self.assertNotIn("Docs: keyword form, keep both sides' lines", ADVICE)

    def test_keeping_both_sides_is_only_ever_mentioned_as_the_thing_not_to_do(self):
        lowered = ADVICE.lower()
        start = 0
        seen = 0
        while True:
            at = lowered.find('keep both sides', start)
            if at < 0:
                break
            seen += 1
            self.assertIn('do not simply', lowered[max(0, at - 20):at],
                          'an unqualified "keep both sides" instruction is the defect')
            start = at + 1
        self.assertEqual(seen, 1, 'the rule must still address the union, not just drop it')

    def test_it_says_the_two_texts_cannot_distinguish_new_from_deleted(self):
        self.assertIn('either new on your branch or deliberately deleted there', ADVICE)
        self.assertIn('cannot tell', ADVICE)

    def test_it_does_not_over_correct_to_the_base_always_winning(self):
        self.assertIn("master's text wins for a row master changed", ADVICE)
        self.assertIn('yours wins for a row your own work changed', ADVICE)


class ConflictAdviceCarriesTheVerification(unittest.TestCase):
    """The audit a correct resolution can actually pass, with the caller's own hash in it.

    It used to demand ZERO deletions. Measured on a staged three-way conflict, that check fails
    the CORRECT resolution (1 deletion), cannot see the resurrection it existed to catch (the
    same 1 deletion; the resurrected row is an ADDITION), and passes the over-correction that
    takes the base wholesale (0 and 0). These tests keep it from coming back.
    """

    def test_it_names_the_diff_against_the_merged_hash(self):
        self.assertIn('git diff 33bfd33 -- <file>', ADVICE)

    def test_it_does_not_demand_zero_deletions(self):
        self.assertNotIn('ZERO deletions', ADVICE)
        self.assertNotIn('zero deletions', ADVICE.lower())

    def test_it_asks_for_every_line_to_be_justified_instead(self):
        self.assertIn('JUSTIFY EVERY LINE', ADVICE)
        self.assertIn('Counting proves nothing', ADVICE)

    def test_it_allows_a_deletion_for_a_row_the_branch_changed(self):
        # The whole clause, not a prefix of it: a prefix survives any edit to the words after it.
        self.assertIn('a deletion is master text you no longer have - fine only for a row your '
                      'own work\n      changed OR RETIRED', ADVICE)

    def test_it_also_allows_a_deletion_for_a_row_the_branch_RETIRED(self):
        """Finished work is deleted, so a row the branch retired is a legitimate deletion of text
        the base still has. Read without this, "changed" excludes a retirement and the reader
        restores the row - the resurrection this whole rule exists to prevent, by the other door.
        """
        self.assertIn('changed OR RETIRED', ADVICE)
        self.assertIn('finished work is deleted here', ADVICE)

    def test_it_reads_the_addition_side_where_a_resurrection_actually_shows(self):
        self.assertIn('an addition is text master does not have', ADVICE)
        self.assertIn('a row master retired, coming back', ADVICE)

    def test_it_names_an_empty_diff_as_the_over_correction(self):
        self.assertIn('NOTHING on either side', ADVICE)
        self.assertIn('your own work is gone', ADVICE)

    def test_the_per_row_fallback_is_offered_against_the_base_branch(self):
        self.assertIn('git log -S"<ROW-ID>" master -- <file>', ADVICE)

    def test_the_base_branch_name_is_not_hard_coded(self):
        main_side = syncbase.conflict_advice('main', 'abcdef1234', 'Some Model', armed=True)
        self.assertIn("main's text wins for a row main changed", main_side)
        self.assertIn('git diff abcdef1 -- <file>', main_side)
        self.assertNotIn('master', main_side)
        other = syncbase.conflict_advice('trunk', 'abcdef1234', 'Some Model', armed=True)
        self.assertIn("trunk's text wins for a row trunk changed", other)
        self.assertNotIn('main', other)

    def test_an_unarmed_message_says_to_add_the_trailer_by_hand(self):
        text = syncbase.conflict_advice('main', 'abcdef1234', 'Some Model', armed=False)
        self.assertIn('could NOT be written into the prepared message', text)
        self.assertIn('Co-Authored-By: Claude Some Model <noreply@anthropic.com>` by hand', text)


class TheP27RefusalText(unittest.TestCase):
    """Refusal-log P27: an agent synced to the local base hash while a lander's merge was unpushed.
    The refusal must send it to the fetched remote hash, never back to local main."""

    def test_it_names_ls_remote_and_never_local_base(self):
        text = syncbase.not_on_remote('a' * 40, 'origin/main', 'origin', 'main', 'b' * 40)
        self.assertIn('is not on origin/main', text)
        self.assertIn('`git ls-remote origin main`', text)
        self.assertIn('never local main', text)
        self.assertIn('it is at bbbbbbb', text)


class Regrown(unittest.TestCase):
    def test_the_thresholds(self):
        self.assertTrue(syncbase.regrown(5000, 500, 2000))
        self.assertFalse(syncbase.regrown(5000, 500, 600), 'merge result not big again')
        self.assertFalse(syncbase.regrown(1000, 800, 2000), 'the base shrank it only 200 bytes')
        self.assertFalse(syncbase.regrown(5000, 4600, 9000), 'the base kept over 90%')


class Arguments(unittest.TestCase):
    """--model required and never blank (the flag, else CLAUDE_AGENT_MODEL), --base optional,
    the trailer always naming Claude."""

    def test_model_is_required_and_never_blank(self):
        self.assertEqual(syncbase.resolve_model(None, {}), (None, None))
        self.assertEqual(syncbase.resolve_model(' ', {}), (None, None))
        self.assertEqual(syncbase.resolve_model(None, {'CLAUDE_AGENT_MODEL': ' '}), (None, None))

    def test_the_flag_wins_over_the_environment(self):
        self.assertEqual(syncbase.resolve_model('M', {'CLAUDE_AGENT_MODEL': 'E'}), ('M', '--model'))
        self.assertEqual(syncbase.resolve_model(None, {'CLAUDE_AGENT_MODEL': 'E'}),
                         ('E', 'CLAUDE_AGENT_MODEL'))

    def test_base_is_optional_and_taken_as_given(self):
        ap = cli.build_parser()
        self.assertIsNone(ap.parse_args(['abc', '--model', 'M']).base)
        self.assertEqual(ap.parse_args(['abc', '--model', 'M', '--base', 'trunk']).base, 'trunk')

    def test_minus_c_defaults_to_here(self):
        self.assertEqual(cli.build_parser().parse_args(['abc']).root, '.')

    def test_a_prefix_of_a_flag_is_not_taken_as_the_flag(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as ctx:
            cli.build_parser().parse_args(['abc', '--mod', 'M'])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn('USAGE:', err.getvalue())

    def test_the_trailer_always_names_claude(self):
        want = 'Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>'
        self.assertEqual(syncbase.trailer('Opus 5'), want)
        self.assertEqual(syncbase.trailer('Claude Opus 5'), want)
        self.assertEqual(syncbase.trailer(' Claude  Opus 5 '), want)


class TheLastParagraph(unittest.TestCase):
    def test_the_line_appearing_only_in_the_body_does_not_count(self):
        """A `merge.log` shortlog or a draft can quote the trailer without it being one."""
        line = syncbase.trailer('Test Model')
        body = ('Merge commit x into work\n\n* an earlier commit that said\n  %s\n\n'
                '* another commit\n' % line)
        self.assertNotIn(line, syncbase.last_paragraph(body))

    def test_a_comment_block_does_not_end_the_message_for_trailer_purposes(self):
        text = 'Merge x into y\n\nCo-Authored-By: M <noreply@anthropic.com>\n\n# Conflicts:\n#\ta\n'
        self.assertIn('Co-Authored-By: M <noreply@anthropic.com>', syncbase.last_paragraph(text))


class TheDocstringAgreesWithTheAdvice(unittest.TestCase):
    """The usage text must not carry a retired rule."""

    def test_the_usage_text_does_not_prescribe_a_union(self):
        for doc in (syncbase.__doc__, cli.__doc__):
            self.assertNotIn("keep the keyword\n  form and both sides' lines", doc)

    def test_the_usage_text_points_at_where_the_rule_lives(self):
        for doc in (syncbase.__doc__, cli.__doc__):
            self.assertIn('conflict_advice()', doc)

    def test_the_usage_text_no_longer_advertises_a_zero_deletions_check(self):
        for doc in (syncbase.__doc__, cli.__doc__):
            self.assertNotIn('zero-deletions', doc)
            self.assertIn('per-line audit', doc)

    def test_conflict_advice_records_why_zero_deletions_was_wrong(self):
        doc = syncbase.conflict_advice.__doc__
        self.assertIn('blind to the resurrection it was introduced to catch', doc)
        self.assertIn('hands a clean bill', doc)

    def test_the_trailer_placement_reason_is_the_measured_one(self):
        doc = syncbase.arm_merge_msg.__doc__
        self.assertIn('git\'s trailer parser ignores a trailing comment-only block', doc,
                      'the `# Conflicts:` block does NOT hide a trailer')
        self.assertIn('merge.log', doc, 'the real reason to append is a NON-comment last paragraph')
        self.assertIn('AUTO-GENERATED message, which never contained a trailer', doc,
                      'that, not placement, is why merges landed untrailered')


if __name__ == '__main__':
    unittest.main()
