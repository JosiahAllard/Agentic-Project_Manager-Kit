"""kitlib.ledger.fmt and kitlib.ledger.write: the rulings-ledger writer (add, retire, check).

Ids are built by rid(n), so this file holds no ruling id of its own (tests/scrub)."""
import os
import shutil
import tempfile
import unittest

from kitlib import ledger
from kitlib.backlog import ledger as backlog_ledger
from kitlib.ledger import fmt, write

WHEN = '2026-10-06 08:00Z'


def rid(n):
    return 'R-%04d' % n


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-ledger-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, 'rulings', 'LEDGER.md')

    def add(self, repos=None, **kw):
        args = dict(repo='alpha', prov='selected', q='Which?', a='This one', decides='Use this one.',
                    source='PM session chat', when=WHEN)
        args.update(kw)
        return write.add(self.path, repos=repos, **args)

    def text(self):
        with open(self.path, encoding='utf-8', newline='') as f:
            return f.read()


class AddTest(Base):
    def test_first_add_starts_from_the_template_and_numbers_from_one(self):
        got = self.add()
        self.assertEqual(got.id, rid(1))
        text = self.text()
        self.assertTrue(text.startswith(fmt.template_text().rstrip('\n')))
        self.assertIn('## %s | %s | alpha | selected\nQ: Which?\nA: This one\nDecides: Use this one.\n'
                      'Source: PM session chat\nSupersedes: -\n' % (rid(1), WHEN), text)
        self.assertNotIn('\r', text)
        self.assertEqual(fmt.validate(text), [])

    def test_ids_run_on_from_the_last(self):
        self.add()
        self.add(decides='Second.')
        self.assertEqual(self.add(decides='Third.').id, rid(3))
        self.assertEqual([i for i, _ in fmt.split_entries(self.text())[1]], [rid(1), rid(2), rid(3)])

    def test_empty_fields_are_usage_errors_and_write_nothing(self):
        for field in ('q', 'a', 'decides', 'source'):
            with self.assertRaises(write.Usage, msg=field):
                self.add(**{field: '  '})
        self.assertFalse(os.path.exists(self.path))

    def test_repo_column_comes_from_the_configured_list_plus_all(self):
        self.add(repos=['alpha', 'beta'], repo='beta')
        self.add(repos=['alpha', 'beta'], repo='all')
        with self.assertRaises(write.Usage):
            self.add(repos=['alpha', 'beta'], repo='gamma')
        self.add(repos=None, repo='gamma')          # unset: any well-formed name
        for bad in ('Alpha', 'a b', 'x|y', ''):
            with self.assertRaises(write.Usage, msg=bad):
                self.add(repo=bad)

    def test_unknown_provenance_and_bad_when_are_usage_errors(self):
        with self.assertRaises(write.Usage):
            self.add(prov='mystery')
        for bad in ('2026-10-06', '2026-10-06 08:00', 'now'):
            with self.assertRaises(write.Usage, msg=bad):
                self.add(when=bad)

    def test_when_defaults_to_now_utc(self):
        self.add(when=None)
        header = self.text().split('\n## ', 1)[1].split('\n', 1)[0]
        self.assertRegex(header, r'^R-\d{4} \| \d{4}-\d{2}-\d{2} \d{2}:\d{2}Z \| alpha \| selected$')

    def test_items_are_the_last_line_and_the_backlog_reads_them(self):
        self.add(items=['T3', 'WG-UNLOCKS'])
        self.assertIn('Supersedes: -\nItems: T3, WG-UNLOCKS\n', self.text())
        found, why = backlog_ledger.read_entries(self.path)
        self.assertIsNone(why)
        self.assertEqual(found[rid(1)].items, ('T3', 'WG-UNLOCKS'))
        self.assertEqual((found[rid(1)].repo, found[rid(1)].kind), ('alpha', 'selected'))

    def test_bad_or_repeated_items_are_refused(self):
        for items in (['ok', 'not ok'], ['A', 'A']):
            with self.assertRaises(write.Refused, msg=items):
                self.add(items=items)

    def test_multi_line_values_are_indented_and_read_back(self):
        self.add(q='line one\nline two')
        self.assertIn('Q: line one\n  line two\n', self.text())
        fields, errs = fmt.parse_block(fmt.split_entries(self.text())[1][0][1])
        self.assertEqual(errs, [])
        self.assertEqual(fields['Q'], 'line one\nline two')

    def test_delegated_needs_an_earlier_owner_entry(self):
        self.add()
        before = self.text()
        for source in ('consultant', 'consultant under %s' % rid(7)):
            with self.assertRaises(write.Refused, msg=source):
                self.add(prov='delegated', source=source)
        self.assertEqual(self.text(), before)
        got = self.add(prov='delegated', source='consultant under %s' % rid(1))
        self.assertEqual(got.id, rid(2))
        with self.assertRaises(write.Refused):        # a delegated entry is not a delegation
            self.add(prov='delegated', source='consultant under %s' % rid(2))

    def test_supersedes_must_name_an_earlier_entry(self):
        self.add()
        self.add(supersedes=rid(1))
        with self.assertRaises(write.Refused):
            self.add(supersedes=rid(9))

    def test_the_reader_lists_what_the_writer_wrote(self):
        self.add()
        self.add(prov='delegated', source='consultant under %s' % rid(1), decides='Use a record.')
        self.assertEqual(ledger.list_rulings(self.text(), 'delegated', '2026-10-06'),
                         ['%s | 2026-10-06 | Use a record.' % rid(2)])

    def test_an_invalid_ledger_is_never_appended_to(self):
        os.makedirs(os.path.dirname(self.path))
        bad = fmt.template_text() + '\n## %s | %s | alpha | selected\nQ: q\n' % (rid(1), WHEN)
        with open(self.path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(bad)
        with self.assertRaises(write.Refused) as cm:
            self.add()
        self.assertIn('already invalid', str(cm.exception))
        self.assertEqual(self.text(), bad)

    def test_a_long_decides_warns_but_writes(self):
        got = self.add(decides='x' * (fmt.DECIDES_NUDGE + 1))
        self.assertEqual(got.id, rid(1))
        self.assertTrue(any('Decides' in w for w in got.warnings))
        self.assertEqual(self.add().warnings, [])

    def test_crlf_ledger_is_read_and_written_lf(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w', encoding='utf-8', newline='\r\n') as f:
            f.write(fmt.template_text())
        self.add()
        self.assertNotIn('\r', self.text())


class ValidateTest(Base):
    def test_ids_must_run_in_order_from_one(self):
        self.add()
        self.add()
        text = self.text().replace('## %s |' % rid(2), '## %s |' % rid(3))
        self.assertTrue(any('expected %s' % rid(2) in e for e in fmt.validate(text)))

    def test_repo_column_checked_against_the_list_when_given(self):
        self.add(repo='gamma')
        self.assertEqual(fmt.validate(self.text()), [])
        self.assertTrue(any('gamma' in e for e in fmt.validate(self.text(), repos=['alpha'])))
        self.assertEqual(fmt.validate(self.text(), repos=['gamma']), [])

    def test_unexpected_and_duplicate_lines(self):
        self.add()
        text = self.text().replace('Supersedes: -\n', 'Supersedes: -\nNote: hi\nQ: again\n')
        errs = fmt.validate(text)
        self.assertTrue(any('unexpected line' in e for e in errs))
        self.assertTrue(any('duplicate field Q' in e for e in errs))


class AppendOnlyTest(Base):
    def test_edits_and_removals_are_refused(self):
        self.add()
        self.add(decides='Second.')
        old = self.text()
        self.assertEqual(fmt.check_append_only(old, old), [])
        self.assertEqual(fmt.check_append_only(None, old), [])
        edited = old.replace('Second.', 'Changed.')
        self.assertTrue(any('was edited' in e for e in fmt.check_append_only(old, edited)))
        removed = old[:old.index('## %s' % rid(2))]
        self.assertTrue(any('was removed' in e for e in fmt.check_append_only(old, removed)))

    def test_tombstoning_is_allowed_and_untombstoning_is_not(self):
        self.add()
        self.add(decides='Second.')
        old = self.text()
        write.retire(self.path, rid(1), 'spent', 'done once')
        new = self.text()
        self.assertEqual(fmt.check_append_only(old, new), [])
        self.assertTrue(any('was edited' in e for e in fmt.check_append_only(new, old)))


class RetireTest(Base):
    def test_retire_turns_the_body_into_one_line_and_the_reader_skips_it(self):
        self.add()
        self.add(decides='Second.')
        write.retire(self.path, rid(1), 'superseded', 'replaced', by=rid(2))
        text = self.text()
        self.assertIn('## %s | %s | alpha | selected\nRetired: superseded - replaced (see %s)\n'
                      % (rid(1), WHEN, rid(2)), text)
        self.assertEqual(fmt.validate(text), [])
        found, _ = backlog_ledger.read_entries(self.path)
        self.assertEqual(found[rid(1)].retired, ('superseded', rid(2)))
        self.assertEqual(ledger.list_rulings(text, 'selected', '2000-01-01'),
                         ['%s | 2026-10-06 | Second.' % rid(2)])

    def test_refusals(self):
        self.add()
        with self.assertRaises(write.Refused):
            write.retire(self.path, rid(5), 'spent', 'why')
        with self.assertRaises(write.Usage):
            write.retire(self.path, rid(1), 'spent', ' ')
        with self.assertRaises(write.Usage):
            write.retire(self.path, rid(1), 'gone', 'why')
        write.retire(self.path, rid(1), 'spent', 'why')
        with self.assertRaises(write.Refused):
            write.retire(self.path, rid(1), 'spent', 'again')

    def test_an_entry_a_live_decides_names_is_not_retired_unless_that_entry_orders_it(self):
        self.add()
        self.add(decides='Builds on %s.' % rid(1))
        before = self.text()
        with self.assertRaises(write.Refused) as cm:
            write.retire(self.path, rid(1), 'spent', 'why')
        self.assertIn('still cited', str(cm.exception))
        self.assertEqual(self.text(), before)
        with self.assertRaises(write.Refused):          # names it, but does not order it retired
            write.retire(self.path, rid(1), 'spent', 'why', ruling=rid(2))
        self.add(decides='Retire %s now.' % rid(1))
        with self.assertRaises(write.Refused):          # rid(2) still cites it
            write.retire(self.path, rid(1), 'spent', 'why', ruling=rid(3))

    def test_the_ordering_ruling_is_exempt(self):
        self.add()
        self.add(decides='Retire %s now.' % rid(1))
        write.retire(self.path, rid(1), 'spent', 'ordered', ruling=rid(2))
        self.assertIn('Retired: spent - ordered (see -)', self.text())


class ShowGrepTest(Base):
    def test_show_and_grep(self):
        self.add()
        self.add(decides='Second thing.')
        text = self.text()
        self.assertTrue(write.show(text, rid(2)).startswith('## %s |' % rid(2)))
        self.assertIn('Second thing.', write.show(text, rid(2)))
        self.assertIsNone(write.show(text, rid(9)))
        self.assertEqual(write.grep(text, 'SECOND'), ['%s | %s | alpha | selected' % (rid(2), WHEN)])
        self.assertEqual(write.grep(text, 'nowhere'), [])


class CheckTest(Base):
    def test_check_combines_validate_and_append_only(self):
        self.add()
        old = self.text()
        self.assertEqual(write.check(old, old), [])
        self.assertEqual(write.check(old, None), [])
        bad = old.replace('Use this one.', 'Other.')
        self.assertTrue(any('was edited' in e for e in write.check(bad, old)))
        self.assertTrue(any('alpha' in e for e in write.check(old, None, repos=['beta'])))


if __name__ == '__main__':
    unittest.main()
