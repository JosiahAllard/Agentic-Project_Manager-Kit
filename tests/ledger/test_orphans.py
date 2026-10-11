"""kitlib.ledger.orphans.scan (pure) and `ledger.py check`'s orphan WARN end to end."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from kitlib.ledger import orphans, read
from tests.backlog.helpers import record_text
from tests.gitfixture import TempRepo
from kitlib.config import user as kuser

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'ledger.py')

TEMPLATE = """# Rulings ledger

## @1 | 2026-10-01 08:00Z | alpha | selected
Q: q
A: a
Decides: Price the open and the closed ones.
Source: chat
Supersedes: -
Items: EX-OPEN-ONE, EX-CLOSED-ONE

## @2 | 2026-10-02 08:00Z | alpha | selected
Q: q
A: a
Decides: Names a record nobody has.
Source: chat
Supersedes: -
Items: EX-OPEN-ONE, EX-TYPO-ONE, EX-TYPO-TWO

## @3 | 2026-10-03 08:00Z | beta | selected
Q: q
A: a
Decides: Another repository's.
Source: chat
Supersedes: -
Items: NOBODY

## @4 | 2026-10-04 08:00Z | all | selected
Q: q
A: a
Decides: For every repository.
Source: chat
Supersedes: -
Items: EX-ALSO-NOBODY

## @5 | 2026-10-05 08:00Z | alpha | selected
Q: q
A: a
Decides: No Items line.
Source: chat
Supersedes: -

## @6 | 2026-10-06 08:00Z | alpha | selected
Retired: spent - done (see -)
"""


def rid(n):
    return 'R-%04d' % n


def fill(text):
    """Ids are built (tests/scrub): `@n` is rid(n)."""
    return re.sub(r'@(\d)', lambda m: rid(int(m.group(1))), text)


LEDGER = fill(TEMPLATE)


class ScanTest(unittest.TestCase):
    def scan(self, repos=('alpha',)):
        entries = {e.id: e for e in read.parse(LEDGER)}
        return orphans.scan(entries, repos, {'EX-OPEN-ONE'}, lambda rid: rid == 'EX-CLOSED-ONE')

    def test_only_this_repositorys_and_all_entries_with_items_are_read(self):
        # The first, second and fourth entries: not beta's, not the one with no Items, not the tombstone.
        self.assertEqual(self.scan().subjects, 3)

    def test_an_id_open_nowhere_is_warned_in_ledger_order(self):
        got = self.scan()
        self.assertEqual(len(got.warnings), 2)
        self.assertTrue(got.warnings[0].startswith(rid(2) + ': Items: EX-OPEN-ONE, EX-TYPO-ONE, EX-TYPO-TWO - '))
        self.assertIn('`EX-TYPO-ONE`, `EX-TYPO-TWO` are', got.warnings[0])
        self.assertTrue(got.warnings[1].startswith(rid(4) + ': '))
        self.assertIn('`EX-ALSO-NOBODY` is', got.warnings[1])

    def test_an_open_or_closed_id_is_no_orphan(self):
        self.assertFalse([w for w in self.scan().warnings if w.startswith(rid(1))])

    def test_a_different_repository_reads_its_own_entries(self):
        got = self.scan(('beta',))
        self.assertEqual((got.subjects, len(got.warnings)), (2, 2))
        self.assertTrue(got.warnings[0].startswith(rid(3)))


class CheckToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.ledger = os.path.join(self.home, 'LEDGER.md')
        with open(self.ledger, 'w', encoding='utf-8', newline='\n') as f:
            f.write(LEDGER)
        os.makedirs(kuser.config_dir(self.home))
        with open(kuser.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump({'ledger_path': self.ledger, 'ledger_repos': ['alpha', 'beta']}, f)
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)

    def check(self, *args, cwd=None):
        env = dict(os.environ, USERPROFILE=self.home, HOME=self.home)
        return subprocess.run([sys.executable, TOOL, 'check', '--base', 'none'] + list(args),
                              capture_output=True, text=True, env=env, cwd=cwd or self.home,
                              timeout=120)

    def kit_json(self):
        self.repo.write('.kit.json', json.dumps({'repo': {'name': 'alpha'},
                                                 'backlog': {'store': 'main'}}))
        self.repo.commit('config', '.kit.json')

    def store(self):
        self.kit_json()
        for rid in ('EX-OPEN-ONE', 'EX-CLOSED-ONE'):
            self.repo.write('docs/backlog/%s.md' % rid, record_text(id=rid))
        self.repo.commit('add records', 'docs/backlog/EX-OPEN-ONE.md', 'docs/backlog/EX-CLOSED-ONE.md')
        self.repo.git('rm', '-q', 'docs/backlog/EX-CLOSED-ONE.md')
        self.repo.git('commit', '-q', '-m', 'closes EX-CLOSED-ONE: done')

    def test_orphans_warn_and_do_not_fail(self):
        self.store()
        r = self.check('--root', self.repo.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('WARN %s: Items: EX-OPEN-ONE, EX-TYPO-ONE, EX-TYPO-TWO' % rid(2), r.stdout)
        self.assertIn('WARN %s: Items: EX-ALSO-NOBODY' % rid(4), r.stdout)
        self.assertNotIn('WARN %s' % rid(1), r.stdout)
        self.assertIn('ruling orphans   3 Items line(s) read, 2 WARN', r.stdout)
        self.assertIn('ledger: 6 entries, OK', r.stdout)

    def test_the_current_directory_is_the_default_root(self):
        self.store()
        r = self.check(cwd=self.repo.root)
        self.assertIn('2 WARN', r.stdout)

    def test_outside_a_repository_the_scan_is_skipped_and_the_check_passes(self):
        r = self.check('--root', self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('ruling orphans   SKIPPED - ', r.stdout)
        self.assertIn('ledger: 6 entries, OK', r.stdout)

    def test_a_repository_with_no_backlog_store_is_skipped(self):
        self.kit_json()
        r = self.check('--root', self.repo.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('SKIPPED - ', r.stdout)
        self.assertIn('holds no backlog store', r.stdout)

    def test_a_malformed_ledger_still_fails_and_still_scans(self):
        self.store()
        with open(self.ledger, 'a', encoding='utf-8', newline='\n') as f:
            f.write('\n## @9 | 2026-10-09 08:00Z | alpha | selected\nQ: q\n')
        r = self.check('--root', self.repo.root)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('FAIL ', r.stdout)
        self.assertIn('ruling orphans', r.stdout)


if __name__ == '__main__':
    unittest.main()
