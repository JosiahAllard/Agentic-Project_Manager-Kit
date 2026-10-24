"""`uncite` (docs/backlog.md "Uncite") on the `summary` and `ref` fields and on the `Blocked on`
body lines `block --reason` writes: each is rewritten into plain words the way a body line is,
a value no rule words cleanly is kept and listed under NEEDS HAND EDIT, and a `Blocked on` line
that is a deliberate or owner hold's only citation is kept and listed. A second run writes
nothing."""
import unittest

from kitlib.backlog import plan as pmod

from tests.backlog.helpers import StoreCase, rec


class BlockLine(unittest.TestCase):
    """uncite_line on a `Blocked on M (date): T` line words only the reason T."""

    def test_the_reason_is_worded_and_the_prefix_kept(self):
        line = 'Blocked on owner (2026-01-01): per R-9033'
        after = 'Blocked on owner (2026-01-01): per an owner ruling'
        self.assertEqual(pmod.uncite_line(line), (after, False))
        self.assertEqual(pmod.uncite_line(after), (after, False))

    def test_a_reason_no_rule_words_is_left(self):
        line = 'Blocked on owner (2026-01-01): R-9035 says wait'
        self.assertEqual(pmod.uncite_line(line), (line, True))

    def test_a_reason_of_only_cites_is_left(self):
        # the line would say `Blocked on M (date): ` and nothing: why is the hand edit's to word
        line = 'Blocked on deliberate (2026-01-01): (R-9035)'
        self.assertEqual(pmod.uncite_line(line), (line, True))

    def test_a_line_with_no_cite_is_untouched(self):
        line = 'Blocked on live-client (2026-01-01): T-1234 first'
        self.assertEqual(pmod.uncite_line(line), (line, False))


class Store(StoreCase):
    remote = False

    def hand_tail(self, change):
        start = next(i for i, l in enumerate(change.lines) if l.startswith('NEEDS HAND EDIT'))
        return change.lines[start + 1:]

    def test_summary_and_ref_are_worded(self):
        self.seed(rec('AAA', summary='Move the hub per R-9033.', ref='notes (R-9034)'))
        change = self.verb('uncite')
        a = self.get('AAA')
        self.assertEqual(a.get('summary'), 'Move the hub per an owner ruling.')
        self.assertEqual(a.get('ref'), 'notes')
        out = '\n'.join(change.lines)
        self.assertIn('    - summary: Move the hub per R-9033.', out)
        self.assertIn('    + summary: Move the hub per an owner ruling.', out)
        self.assertIn('    - ref: notes (R-9034)\n    + ref: notes', out)
        self.assertIn('2 cite(s) in 1 record(s)', change.lines[0])
        self.assertIn('2 field(s) rewritten', change.lines[0])

    def test_a_ref_of_only_cites_is_dropped(self):
        self.seed(rec('AAA', ref='R-9034'))
        change = self.verb('uncite')
        self.assertIsNone(self.get('AAA').get('ref'))
        self.assertIn('    - ref: R-9034\n    + ref: (dropped)', '\n'.join(change.lines))

    def test_a_field_no_rule_words_is_kept_and_listed(self):
        self.seed(rec('AAA', summary='R-9033 says move the hub', ref='R-9004 notes'),
                  rec('BBB', summary='(R-9033)'))
        before = self.store.load()
        change = self.verb('uncite')
        self.assertEqual(change.writes, {})
        for rid in ('AAA', 'BBB'):
            self.assertEqual(self.get(rid).text, before.get(rid).text)
        where = before.where
        a_lines = before.get('AAA').text.split('\n')
        b_lines = before.get('BBB').text.split('\n')
        self.assertEqual(self.hand_tail(change), [
            '  %s AAA line %d (summary): R-9033 says move the hub'
            % (where, a_lines.index('summary: R-9033 says move the hub') + 1),
            '  %s AAA line %d (ref): R-9004 notes'
            % (where, a_lines.index('ref: R-9004 notes') + 1),
            # a summary is required: one that is only cites is left for a hand edit, not dropped
            '  %s BBB line %d (summary): (R-9033)'
            % (where, b_lines.index('summary: (R-9033)') + 1)])

    def test_blocked_on_lines_are_worded(self):
        self.seed(rec('AAA', state='blocked', blocked_on='live-client',
                      body='Blocked on live-client (2026-01-01): until the build, per R-9035'))
        self.verb('uncite')
        self.assertEqual(self.get('AAA').body.strip(),
                         'Blocked on live-client (2026-01-01): until the build')

    def test_a_holds_only_citation_is_kept_and_listed(self):
        line = 'Blocked on deliberate (2026-01-01): until the rewrite lands, per R-9035'
        self.seed(rec('AAA', state='blocked', blocked_on='deliberate', body=line),
                  # another citation holds it: its Blocked on line is worded
                  rec('BBB', state='blocked', blocked_on='deliberate', q='Q-301',
                      body=line.replace('R-9035', 'R-9036')))
        before = self.store.load()
        change = self.verb('uncite')
        self.assertEqual(sorted(change.writes), ['BBB'])
        self.assertEqual(self.get('AAA').text, before.get('AAA').text)
        self.assertEqual(self.get('BBB').body.strip(),
                         'Blocked on deliberate (2026-01-01): until the rewrite lands')
        n = before.get('AAA').text.split('\n').index(line) + 1
        self.assertEqual(self.hand_tail(change), [
            "  %s AAA line %d: %s (the hold's only citation)" % (before.where, n, line)])

    def test_a_hold_line_still_citing_after_wording_is_worded(self):
        # the T- id still cites the hold once R-9035 is worded: the guard does not keep it
        self.seed(rec('AAA', state='blocked', blocked_on='deliberate',
                      body='Blocked on deliberate (2026-01-01): until T-1234 is done, per R-9035'))
        change = self.verb('uncite')
        self.assertEqual(sorted(change.writes), ['AAA'])
        self.assertEqual(self.get('AAA').body.strip(),
                         'Blocked on deliberate (2026-01-01): until T-1234 is done')
        self.assertNotIn('NEEDS HAND EDIT', '\n'.join(change.lines))

    def test_a_second_run_changes_nothing(self):
        self.seed(rec('AAA', summary='Move the hub per R-9033.', ref='R-9034',
                      state='blocked', blocked_on='live-client',
                      body='Blocked on live-client (2026-01-01): per R-9035'),
                  rec('BBB', summary='R-9033 says move it', state='blocked',
                      blocked_on='deliberate',
                      body='Blocked on deliberate (2026-01-01): per R-9036'))
        first = self.verb('uncite')
        self.assertEqual(sorted(first.writes), ['AAA'])
        snap = self.store.load()
        tip = self.remote_tip()
        again = self.verb('uncite')
        self.assertEqual(again.writes, {})
        self.assertEqual(self.remote_tip(), tip)
        self.assertIn('nothing to rewrite', again.lines[0])
        self.assertEqual(self.hand_tail(again), self.hand_tail(first))
        for rid in ('AAA', 'BBB'):
            self.assertEqual(self.get(rid).text, snap.get(rid).text)


if __name__ == '__main__':
    unittest.main()
