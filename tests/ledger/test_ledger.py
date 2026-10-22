"""kitlib.ledger.listing and tools/ledger.py list: the delegated-rulings digest feed; and
kitlib.ledger.fmt.validate's `under` anchor of a delegated entry."""
import os
import subprocess
import sys
import tempfile
import unittest

from kitlib import ledger
from kitlib.config import user as kuser
from kitlib.ledger import fmt

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'ledger.py')

TEXT = """# Rulings ledger

Preamble line.

## R-9001 | 2026-09-24 09:41Z | alpha | selected
Retired: spent - one-off (see -)

## R-9002 | 2026-10-05 23:59Z | pm | delegated
Q: q
A: PM (R-9099): a
Decides: Old delegated ruling.
Source: PM s under R-9099
Supersedes: -

## R-9003 | 2026-10-06 00:00Z | alpha | selected
Q: q
A: a
Decides: Owner ruling, not delegated.
Source: PM s
Supersedes: -

## R-9004 | 2026-10-06 08:00Z | alpha | delegated
Q: q
A: PM (R-9099): b
Decides: Use a record, not a class.
Source: PM s under R-9099
Supersedes: -
Items: EX-API-1

## R-9005 | 2026-10-07 12:28Z | all | delegated
Q: q
A: PM (R-9099): c
Source: PM s under R-9099
Supersedes: -

## R-9006 | 2026-10-07 13:00Z | all | delegated
Retired: superseded - replaced (see R-9007)
"""


class ListTest(unittest.TestCase):
    def test_filters_by_prov_and_since_inclusive(self):
        lines = ledger.list_rulings(TEXT, 'delegated', '2026-10-06')
        self.assertEqual(lines, ['R-9004 | 2026-10-06 | Use a record, not a class.',
                                 'R-9005 | 2026-10-07 | (no Decides)'])

    def test_since_before_everything_includes_older_ones_in_order(self):
        lines = ledger.list_rulings(TEXT, 'delegated', '2026-01-01')
        self.assertEqual([l.split(' ')[0] for l in lines], ['R-9002', 'R-9004', 'R-9005'])

    def test_other_prov(self):
        self.assertEqual(ledger.list_rulings(TEXT, 'selected', '2026-10-06'),
                         ['R-9003 | 2026-10-06 | Owner ruling, not delegated.'])

    def test_tombstones_are_skipped(self):
        self.assertFalse(any('R-9006' in l or 'R-9001' in l for l in ledger.list_rulings(TEXT, 'delegated', '2000-01-01')))

    def test_bad_prov_or_date_raises(self):
        with self.assertRaises(ValueError):
            ledger.list_rulings(TEXT, 'bogus', '2026-10-06')
        with self.assertRaises(ValueError):
            ledger.list_rulings(TEXT, 'delegated', '10/06/2026')


class CliTest(unittest.TestCase):
    def run_tool(self, args, config):
        with tempfile.TemporaryDirectory() as d:
            if config is not None:
                os.makedirs(kuser.config_dir(d))
                with open(kuser.config_file(d), 'w', encoding='utf-8') as f:
                    f.write(config(d))
            env = dict(os.environ, USERPROFILE=d, HOME=d)
            return subprocess.run([sys.executable, TOOL] + args, capture_output=True, text=True, env=env, timeout=30)

    def with_ledger(self, d):
        path = os.path.join(d, 'LEDGER.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(TEXT)
        return '{"ledger_path": %s}' % __import__('json').dumps(path)

    def test_list_prints_one_line_per_ruling(self):
        r = self.run_tool(['list', '--prov', 'delegated', '--since', '2026-10-06'], self.with_ledger)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ['R-9004 | 2026-10-06 | Use a record, not a class.',
                                                 'R-9005 | 2026-10-07 | (no Decides)'])

    def test_no_ledger_configured_exits_1(self):
        r = self.run_tool(['list', '--prov', 'delegated', '--since', '2026-10-06'], lambda d: '{}')
        self.assertEqual(r.returncode, 1)
        self.assertIn('ledger_path', r.stderr)

    def test_usage_errors_exit_2(self):
        for args in (['list', '--prov', 'nope', '--since', '2026-10-06'], ['list', '--prov', 'delegated', '--since', 'x'],
                     ['list', '--prov', 'delegated']):
            self.assertEqual(self.run_tool(args, self.with_ledger).returncode, 2, args)


class UnreadableAndRefusedTest(CliTest):
    LIST = ['list', '--prov', 'delegated', '--since', '2026-10-06']

    def with_file(self, data):
        def make(d):
            path = os.path.join(d, 'LEDGER.md')
            with open(path, 'wb') as f:
                f.write(data)
            return '{"ledger_path": %s}' % __import__('json').dumps(path)
        return make

    def test_non_utf8_ledger_exits_1_not_2(self):
        r = self.run_tool(self.LIST, self.with_file(b'## R-9001 | 2026-10-06 00:00Z | pm | delegated' + bytes([0xff, 0xfe])))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('cannot read', r.stderr)

    def test_missing_ledger_file_exits_1(self):
        r = self.run_tool(self.LIST, lambda d: '{"ledger_path": %s}' % __import__('json').dumps(os.path.join(d, 'none.md')))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('cannot read', r.stderr)

    def test_bad_machine_config_exits_1(self):
        for body in ('{"ledger_path": "relative.md"}', '{', '{"ledger_pth": "/x"}'):
            r = self.run_tool(self.LIST, lambda d, b=body: b)
            self.assertEqual(r.returncode, 1, (body, r.stderr))

    def test_a_ledger_path_flag_is_rejected(self):
        r = self.run_tool(self.LIST + ['--ledger-path', 'x.md'], self.with_ledger)
        self.assertEqual(r.returncode, 2)
        self.assertIn('USAGE', r.stderr)


class HeaderEdgeTest(unittest.TestCase):
    def test_unknown_prov_header_is_skipped(self):
        text = ('## R-9001 | 2026-10-06 00:00Z | pm | mystery' + chr(10) + 'Decides: x' + chr(10) + chr(10) +
                '## R-9002 | 2026-10-06 00:00Z | pm | delegated' + chr(10) + 'Decides: y' + chr(10))
        self.assertEqual(ledger.list_rulings(text, 'delegated', '2026-10-06'), ['R-9002 | 2026-10-06 | y'])

    def test_since_with_trailing_newline_is_refused(self):
        with self.assertRaises(ValueError):
            ledger.list_rulings('', 'delegated', '2026-10-06' + chr(10))


class UnderAnchorTest(unittest.TestCase):
    """kitlib.ledger.fmt.validate: a delegated entry's Source says `under <id>`, an earlier owner
    entry, or `under hub:A-nnn`, a hub approval, which is its own anchor (docs/ledger.md)."""

    def text(self, *entries, repo=None):
        out = '# Rulings ledger\n\n'
        for n, (prov, source) in enumerate(entries, 1):
            out += fmt.render(fmt.fmt_id(n, repo), '2026-10-20 12:00Z', repo or 'pm', prov,
                              'q', 'a', 'run D-001 dest=x', source, '-')
        return out

    def test_a_delegated_entry_under_a_hub_approval_needs_no_owner_entry(self):
        for source in ('under hub:A-001: D-001 hub:A-001', 'under hub:A-999',
                       'under hub:A-1000: D-001 hub:A-1000', 'run under hub:A-042.'):
            with self.subTest(source=source):
                self.assertEqual(fmt.validate(self.text(('delegated', source))), [])
                self.assertEqual(fmt.validate(self.text(('delegated', source), repo='pm'),
                                              repo='pm'), [])

    def test_a_malformed_hub_anchor_is_refused(self):
        for source in ('under hub:A-01', 'under hub:A-1', 'under hub:A-000', 'under hub:A-0001',
                       'under hub:A-0100', 'under A-001', 'under hub:a-001', 'under hub:B-001',
                       'under hub: A-001', 'under hub:A-001x', 'under hub:A-001-2',
                       'under hubA-001', 'hub:A-001'):
            with self.subTest(source=source):
                self.assertEqual(fmt.validate(self.text(('delegated', source))),
                                 ["R-0001: a delegated entry's Source must say 'under <id>'"])

    def test_the_ruling_id_forms_are_checked_as_before(self):
        self.assertEqual(fmt.validate(self.text(('typed', 'owner'), ('delegated', 'under R-0001'))), [])
        self.assertEqual(fmt.validate(self.text(('selected', 'owner'), ('delegated', 'x under R-0001.'))), [])
        self.assertEqual(fmt.validate(self.text(('typed', 'owner'), ('delegated', 'under H-pm-0001'),
                                                repo='pm'), repo='pm'), [])
        other = self.text(('relayed-from-terminal', 'owner'), repo='alpha')
        self.assertEqual(fmt.validate(self.text(('delegated', 'under H-alpha-0001'), repo='pm'),
                                      repo='pm', others=[other]), [])
        cases = (
            ((('delegated', 'under R-0002'), ('typed', 'owner')),
             'R-0001: delegated under R-0002, which is not an earlier owner entry'),
            ((('typed', 'owner'), ('delegated', 'under R-0001'), ('delegated', 'under R-0002')),
             'R-0003: delegated under R-0002, which is not an earlier owner entry'),
            ((('delegated', 'under R-0009'),),
             'R-0001: delegated under R-0009, which is not an earlier owner entry'),
            ((('typed', 'owner'), ('delegated', 'consultant')),
             "R-0002: a delegated entry's Source must say 'under <id>'"),
            ((('typed', 'owner'), ('delegated', 'under R-001')),
             "R-0002: a delegated entry's Source must say 'under <id>'"),
        )
        for entries, err in cases:
            with self.subTest(entries=entries):
                self.assertEqual(fmt.validate(self.text(*entries)), [err])

    def test_an_owner_entry_needs_no_anchor_and_names_a_hub_approval_freely(self):
        self.assertEqual(fmt.validate(self.text(('selected', 'hub:A-001'))), [])


if __name__ == '__main__':
    unittest.main()
