"""kitlib.maintlint.ledger over the rulebooks (docs/maint-lint.md "Ledger counts"): rules per file,
files over budget, possible-conflict pairs and overrides, as numbers and file names only."""
import json
import os
import tempfile
import unittest

from kitlib.maintlint import ledger, page
from tests.gitfixture import rmtree
from tests.ledger.test_standing import entry


def _id(n):
    return 'H-%s-%04d' % ('a', n)


IDS = {'h1': _id(1), 'h2': _id(2), 'h3': _id(3)}

SHARED = """# Shared rules
Repos: shared
Budget: 40

## Landing
- landing.batch: Landers batch several branches together. (from %(h1)s)
- landing.docs: Documentation accompanies behaviour changes. (from %(h1)s)
""" % IDS

KIT = """# Kit rules
Repos: kit
Budget: 2

## Gate
- gate.batch: Batch branches together through gate runs. (from %(h2)s)
- gate.full: Strict paths need full coverage. (from %(h2)s)

## Landing
- landing.docs: Prose lives beside code. (from %(h3)s)
""" % IDS


def temp_dir(case):
    path = tempfile.mkdtemp(prefix='maintlint-')
    case.addCleanup(rmtree, path)
    return path


def home(test, books):
    folder = temp_dir(test)
    path = os.path.join(folder, 'ledger.md')
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('# Rulings ledger\n\n' + entry(1))
    if books:
        os.makedirs(os.path.join(folder, 'rules'))
    for name, text in books.items():
        with open(os.path.join(folder, 'rules', name + '.md'), 'w', encoding='utf-8',
                  newline='\n') as fh:
            fh.write(text)
    return lambda h: {'ledger_path': path}


class RulebookCounts(unittest.TestCase):

    def test_rules_per_file_over_budget_pairs_and_overrides(self):
        got, notes = ledger.counts(user_config=home(self, {'all': SHARED, 'kit': KIT}))
        self.assertEqual(got['rules'], {'all': 2, 'kit': 3})
        self.assertEqual(got['over_budget'], ['kit'])
        self.assertEqual(got['overrides'], 1)
        self.assertEqual(got['conflict_pairs'], 1)      # landing.batch with gate.batch, once
        self.assertEqual(notes, [])
        self.assertNotIn('superseded_live', got)
        self.assertNotIn('superseded', got)

    def test_no_rulebook_is_zero_counts(self):
        got, _notes = ledger.counts(user_config=home(self, {}))
        self.assertEqual((got['rules'], got['over_budget'], got['conflict_pairs'],
                          got['overrides']), ({}, [], 0, 0))

    def test_a_malformed_rulebook_is_a_note_not_a_crash(self):
        got, notes = ledger.counts(user_config=home(self, {'kit': 'junk\n'}))
        self.assertIsNotNone(got)
        self.assertTrue(any('rulebook' in n for n in notes), notes)

    def test_no_rule_text_or_key_leaves_the_module(self):
        got, _notes = ledger.counts(user_config=home(self, {'all': SHARED, 'kit': KIT}))
        res = {'repo': 'x', 'counts': {}, 'ledger': got, 'notes': [], 'findings': []}
        text = json.dumps(got) + page.render(res) + json.dumps([
            f._asdict() for f in ledger.findings(got)])
        for leak in ('landing.', 'gate.', 'Landers', 'H-', 'R-'):
            self.assertNotIn(leak, text)
        self.assertIn('2 rules in all', page.render(res))


class RulebookFindings(unittest.TestCase):

    def test_over_budget_and_conflicts_are_findings_and_overrides_are_not(self):
        got = ledger.findings({'stale_cites': 0, 'rules': {'all': 2, 'kit': 3},
                               'over_budget': ['kit'], 'conflict_pairs': 2, 'overrides': 1})
        self.assertEqual([(f.check, f.key) for f in got],
                         [('ledger-over-budget', 'over_budget'),
                          ('ledger-rule-conflicts', 'conflict_pairs')])
        self.assertIn('kit', got[0].text)
        self.assertIn('2 possible', got[1].text)
        for f in got:
            self.assertIn('the PM', f.fix)

    def test_nothing_above_0_is_no_finding(self):
        self.assertEqual(ledger.findings({'stale_cites': 0, 'rules': {'kit': 1},
                                          'over_budget': [], 'conflict_pairs': 0,
                                          'overrides': 3}), [])
