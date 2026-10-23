"""tools/ledger.py add/show/grep/check/retire: arguments and exit codes only (the logic is
tests/ledger/test_writer.py's). Ids are built by rid(n) (tests/scrub)."""
import os
import subprocess
import sys
import tempfile
import unittest

from tests.home import machine_config

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'ledger.py')
ADD = ['add', '--repo', 'alpha', '--prov', 'selected', '--q', 'Which?', '--a', 'This',
       '--decides', 'Use this.', '--source', 'chat', '--when', '2026-10-06 08:00Z']


def rid(n):
    return 'R-%04d' % n


class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.ledger = os.path.join(self.home, 'rulings', 'LEDGER.md')
        self.config({'ledger_path': self.ledger, 'ledger_repos': ['alpha']})

    def config(self, data):
        machine_config(self.home, data, case=self)

    def run_tool(self, *args):
        env = dict(os.environ, USERPROFILE=self.home, HOME=self.home, CLAUDE_AGENT_MODEL='')
        return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True,
                              env=env, timeout=60)


class RemovedFlagsTest(CliBase):
    """`add --items` and `check --root` are gone, and `check` runs no orphan scan."""

    def test_add_items_is_a_usage_error_and_writes_nothing(self):
        r = self.run_tool(*(ADD + ['--items', 'X']))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('--items', r.stderr)
        self.assertFalse(os.path.exists(self.ledger))

    def test_check_root_is_a_usage_error(self):
        self.assertEqual(self.run_tool(*ADD).returncode, 0)
        r = self.run_tool('check', '--base', 'none', '--root', self.home)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('--root', r.stderr)

    def test_check_prints_no_orphan_line(self):
        self.assertEqual(self.run_tool(*ADD).returncode, 0)
        r = self.run_tool('check', '--base', 'none')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('orphan', r.stdout + r.stderr)


class CliWriterTest(CliBase):
    def test_add_prints_the_new_id_and_show_grep_check_read_it(self):
        r = self.run_tool(*ADD)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, rid(1)), r.stderr)
        r = self.run_tool('show', rid(1))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Decides: Use this.', r.stdout)
        self.assertEqual(self.run_tool('show', rid(2)).returncode, 1)
        r = self.run_tool('grep', 'use THIS')
        self.assertEqual((r.returncode, r.stdout.strip()),
                         (0, '%s | 2026-10-06 08:00Z | alpha | selected' % rid(1)))
        self.assertEqual(self.run_tool('grep', 'nowhere').returncode, 1)
        r = self.run_tool('check', '--base', 'none')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('ledger: 1 entries, OK', r.stdout)
        self.assertIn('append-only NOT CHECKED', r.stdout)

    def test_repo_outside_the_configured_list_is_a_usage_error(self):
        args = list(ADD)
        args[args.index('alpha')] = 'beta'
        r = self.run_tool(*args)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('alpha', r.stderr)
        self.assertFalse(os.path.exists(self.ledger))

    def test_missing_or_empty_arguments_are_usage_errors(self):
        self.assertEqual(self.run_tool('add', '--repo', 'alpha').returncode, 2)
        args = list(ADD)
        args[args.index('Which?')] = ' '
        self.assertEqual(self.run_tool(*args).returncode, 2)
        self.assertEqual(self.run_tool(*(ADD + ['--when', 'later'])).returncode, 2)

    def test_no_ledger_configured_exits_1(self):
        self.config({})
        for args in (ADD, ['show', rid(1)], ['check']):
            r = self.run_tool(*args)
            self.assertEqual(r.returncode, 1, (args, r.stderr))
            self.assertIn('ledger_path', r.stderr)

    def test_a_bad_ledger_repos_value_exits_1_and_writes_nothing(self):
        self.config({'ledger_path': self.ledger, 'ledger_repos': ['Not A Name']})
        r = self.run_tool(*ADD)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('ledger_repos', r.stderr)
        self.assertFalse(os.path.exists(self.ledger))

    def test_a_refused_entry_exits_1(self):
        self.assertEqual(self.run_tool(*ADD).returncode, 0)
        args = list(ADD)
        args[args.index('selected')] = 'delegated'
        r = self.run_tool(*args)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('under', r.stderr)

    def test_commit_without_a_model_is_a_usage_error_and_writes_nothing(self):
        r = self.run_tool(*(ADD + ['--commit']))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertFalse(os.path.exists(self.ledger))

    def test_check_fails_an_invalid_ledger(self):
        self.assertEqual(self.run_tool(*ADD).returncode, 0)
        with open(self.ledger, 'a', encoding='utf-8') as f:
            f.write('## %s | 2026-10-06 08:00Z | alpha | selected\nQ: only\n' % rid(5))
        r = self.run_tool('check', '--base', 'none')
        self.assertEqual(r.returncode, 1)
        self.assertIn('FAIL ', r.stdout)

    def test_retire_writes_a_tombstone(self):
        self.assertEqual(self.run_tool(*ADD).returncode, 0)
        r = self.run_tool('retire', rid(1), '--class', 'spent', '--why', 'done')
        self.assertEqual((r.returncode, r.stdout.strip()), (0, rid(1)), r.stderr)
        self.assertIn('Retired: spent - done (see -)', self.run_tool('show', rid(1)).stdout)
        self.assertEqual(self.run_tool('retire', rid(1), '--class', 'spent', '--why', 'x').returncode, 1)
        self.assertEqual(self.run_tool('retire', rid(1), '--class', 'gone', '--why', 'x').returncode, 2)

    def test_list_still_reads_what_add_wrote(self):
        self.assertEqual(self.run_tool(*ADD).returncode, 0)
        r = self.run_tool('list', '--prov', 'selected', '--since', '2026-10-06')
        self.assertEqual((r.returncode, r.stdout.strip()), (0, '%s | 2026-10-06 | Use this.' % rid(1)))


if __name__ == '__main__':
    unittest.main()
