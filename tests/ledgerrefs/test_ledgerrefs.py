"""kitlib.ledgerrefs and tools/ledger_refs.py: where each ruling id is cited, in repositories at a
committed ref and in folders on disk (docs/ledger.md "ledger_refs"). Real git in a throwaway
repository (tests/gitfixture.py). Ids are built by rid(n) (tests/scrub)."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from kitlib import ledgerrefs
from kitlib.config import user
from kitlib.config.types import ConfigError
from tests.gitfixture import TempRepo, fixture_env
from tests.realgit import real_git

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'ledger_refs.py')


def rid(n):
    return 'R-%04d' % n


class PatternTest(unittest.TestCase):
    def test_matches_both_spellings_and_never_a_longer_id(self):
        rx = re.compile(ledgerrefs.pattern(rid(12)))
        for hit in ('see %s.' % rid(12), 'R0012 here', 'end %s' % rid(12)):
            self.assertTrue(rx.search(hit), hit)
        for miss in (rid(120), 'R00123', rid(13)):
            self.assertFalse(rx.search(miss), miss)

    def test_a_non_id_is_refused(self):
        for bad in ('R-12', 'X-0012', rid(12) + ' ', ''):
            with self.assertRaises(ValueError):
                ledgerrefs.pattern(bad)


@real_git('git grep: the fake lacks it')
class FindTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('docs/a.md', 'line one\nper %s we do this\n' % rid(5))
        self.repo.write('tools/b.py', '# R0005\n')
        self.repo.commit('cite', 'docs/a.md', 'tools/b.py')
        # uncommitted: not seen at the ref
        self.repo.write('docs/late.md', 'later %s\n' % rid(5))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = os.path.join(self.tmp.name, 'memory')
        os.makedirs(os.path.join(self.folder, 'sub'))
        for rel, text in (('note.md', 'x\n%s\n' % rid(5)), ('sub/skip.bin', rid(5)),
                          ('sub/other.txt', rid(50) + '\n')):
            with open(os.path.join(self.folder, rel), 'w', encoding='utf-8') as f:
                f.write(text)
        self.ledger = os.path.join(self.tmp.name, 'LEDGER.md')
        with open(self.ledger, 'w', encoding='utf-8') as f:
            f.write('## %s | d | all | selected\nSupersedes: -\n\n## %s | d | all | selected\n'
                    'Supersedes: %s\n' % (rid(5), rid(6), rid(5)))
        self.settings = {'repos': {'alpha': {'path': self.repo.root, 'ref': 'HEAD'}},
                         'folders': {'mem': self.folder}}

    def test_hits_in_repos_at_the_ref_and_folders_on_disk(self):
        r = ledgerrefs.find(rid(5), self.settings, self.ledger)
        self.assertEqual(r.hits, ['alpha:docs/a.md:2', 'alpha:tools/b.py:1', 'mem:note.md:2'])
        self.assertEqual(r.ledger_mentions, 1)
        self.assertEqual(r.errors, [])

    def test_no_settings_counts_the_ledger_only(self):
        r = ledgerrefs.find(rid(6), None, self.ledger)
        self.assertEqual((r.hits, r.ledger_mentions, r.errors), ([], 0, []))

    def test_an_extensionless_ledger_is_still_counted(self):
        bare = os.path.join(self.tmp.name, 'LEDGER')
        with open(self.ledger, encoding='utf-8') as src, open(bare, 'w', encoding='utf-8') as f:
            f.write(src.read())
        self.assertEqual(ledgerrefs.find(rid(5), None, bare).ledger_mentions, 1)

    def test_a_repo_git_cannot_search_and_a_missing_folder_are_errors_not_no_hits(self):
        self.settings['repos']['beta'] = {'path': self.repo.root, 'ref': 'no-such-ref'}
        self.settings['folders']['gone'] = os.path.join(self.tmp.name, 'gone')
        r = ledgerrefs.find(rid(5), self.settings, self.ledger)
        self.assertEqual(len(r.hits), 3)
        self.assertEqual(len(r.errors), 2, r.errors)
        self.assertTrue(r.errors[0].startswith('beta: cannot search'), r.errors)
        self.assertTrue(r.errors[1].startswith('gone: '), r.errors)

    def test_machine_config_shape_is_checked(self):
        home = self.tmp.name
        os.makedirs(os.path.join(home, '.claude-pm-kit'))
        path = os.path.join(home, '.claude-pm-kit', 'config.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'ledger_refs': {'repos': {'alpha': {'path': self.repo.root}}}}, f)
        with self.assertRaises(ConfigError):
            user.user_config(home)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'ledger_refs': self.settings}, f)
        self.assertEqual(user.user_config(home)['ledger_refs']['folders'], {'mem': self.folder})

    def run_tool(self, *args):
        home = os.path.join(self.tmp.name, 'home')
        os.makedirs(os.path.join(home, '.claude-pm-kit'), exist_ok=True)
        with open(os.path.join(home, '.claude-pm-kit', 'config.json'), 'w', encoding='utf-8') as f:
            json.dump({'ledger_path': self.ledger, 'ledger_refs': self.settings}, f)
        env = fixture_env({'USERPROFILE': home, 'HOME': home})
        return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True,
                              env=env, timeout=60)

    def test_cli_prints_one_block_per_id_and_exits_by_errors(self):
        ids = os.path.join(self.tmp.name, 'ids.json')
        with open(ids, 'w', encoding='utf-8') as f:
            json.dump([{'id': rid(6)}], f)
        r = self.run_tool(rid(5), '--from-json', ids)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(),
                         ['%s  refs=3  ledger-mentions=1' % rid(5), '  alpha:docs/a.md:2',
                          '  alpha:tools/b.py:1', '  mem:note.md:2',
                          '%s  refs=0  ledger-mentions=0' % rid(6)])
        self.settings['folders']['gone'] = os.path.join(self.tmp.name, 'gone')
        r = self.run_tool(rid(5))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('  ERROR gone: ', r.stdout)

    def test_cli_usage_errors_exit_2(self):
        self.assertEqual(self.run_tool().returncode, 2)
        r = self.run_tool('R-12')
        self.assertEqual(r.returncode, 2)
        self.assertIn('not a ruling id', r.stderr)
        self.assertEqual(self.run_tool('--from-json', os.path.join(self.tmp.name, 'none.json')).returncode, 2)


if __name__ == '__main__':
    unittest.main()
