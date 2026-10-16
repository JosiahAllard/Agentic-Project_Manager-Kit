"""tools/ledger.py with `ledger_dir` set (docs/ledger.md "Files"): arguments, routing and exit codes.
The logic is tests/ledger/test_split.py's. Ids are built by rid(n) and hid(repo, n) (tests/scrub)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from kitlib.config import user as kuser
from kitlib.ledger import fmt
from tests.gitfixture import TempRepo, fixture_env

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'ledger.py')
WHEN = '2026-10-06 08:00Z'


def rid(n):
    return 'R-%04d' % n


def hid(repo, n):
    return 'H-%s-%04d' % (repo, n)


def add_args(repo, decides='Use this.', prov='selected', source='chat', *more):
    return ['add', '--repo', repo, '--prov', prov, '--q', 'Which?', '--a', 'This', '--decides', decides,
            '--source', source, '--when', WHEN] + list(more)


def frozen_text():
    return ('# Ledger\n\n'
            + fmt.render(rid(1), WHEN, 'all', 'selected', 'q', 'a', 'Old rule for all.', 's', '-')
            + fmt.render(rid(2), WHEN, 'beta', 'selected', 'q', 'a', 'Old rule for beta.', 's', '-'))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.dir = os.path.join(self.home, 'rulings')
        os.makedirs(self.dir)
        self.frozen = os.path.join(self.dir, 'LEDGER.md')
        with open(self.frozen, 'w', encoding='utf-8', newline='\n') as f:
            f.write(frozen_text())
        self.config({'ledger_dir': self.dir, 'ledger_repos': ['alpha', 'pm'], 'ledger_path': self.frozen})

    def config(self, data):
        os.makedirs(kuser.config_dir(self.home), exist_ok=True)
        with open(kuser.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def run_tool(self, *args, env=None):
        env = dict(env or os.environ, USERPROFILE=self.home, HOME=self.home)
        env.setdefault('CLAUDE_AGENT_MODEL', '')
        return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True,
                              env=env, timeout=60)

    def ok(self, *args):
        r = self.run_tool(*args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def path(self, repo):
        return os.path.join(self.dir, repo + '.md')

    def read(self, path):
        with open(path, encoding='utf-8') as f:
            return f.read()


class AddTest(Base):
    def test_add_writes_the_repos_file_and_numbers_it(self):
        self.assertEqual(self.ok(*add_args('alpha')).strip(), hid('alpha', 1))
        self.assertEqual(self.ok(*add_args('pm')).strip(), hid('pm', 1))
        self.assertEqual(self.ok(*add_args('alpha')).strip(), hid('alpha', 2))
        self.assertIn('## %s | %s | alpha | selected' % (hid('alpha', 2), WHEN), self.read(self.path('alpha')))
        self.assertEqual(self.read(self.frozen), frozen_text())

    def test_all_and_an_unlisted_repo_are_usage_errors(self):
        r = self.run_tool(*add_args('all'))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('use pm', r.stderr)
        r = self.run_tool(*add_args('beta'))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(sorted(os.listdir(self.dir)), ['LEDGER.md'])

    def test_under_names_another_files_owner_entry_or_the_frozen_ledgers(self):
        self.ok(*add_args('alpha'))
        self.ok(*add_args('pm', 'Follow.', 'delegated', 'under %s' % hid('alpha', 1)))
        self.ok(*add_args('pm', 'Follow.', 'delegated', 'under %s' % rid(1)))
        r = self.run_tool(*add_args('alpha', 'Follow.', 'delegated', 'under %s' % hid('pm', 1)))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('not an earlier owner entry', r.stderr)


class ReadTest(Base):
    def setUp(self):
        super().setUp()
        self.ok(*add_args('alpha', 'Alpha rule.'))
        self.ok(*add_args('pm', 'Pm rule.'))

    def test_show_routes_by_id(self):
        self.assertIn('Decides: Alpha rule.', self.ok('show', hid('alpha', 1)))
        self.assertIn('Decides: Old rule for all.', self.ok('show', rid(1)))
        for missing in (hid('alpha', 9), hid('gamma', 1), rid(9), 'nonsense'):
            self.assertEqual(self.run_tool('show', missing).returncode, 1, missing)

    def test_grep_reads_every_file_or_one_repos_view(self):
        self.assertEqual(self.ok('grep', 'RULE').splitlines(),
                         ['%s | %s | all | selected' % (rid(1), WHEN), '%s | %s | beta | selected' % (rid(2), WHEN),
                          '%s | %s | alpha | selected' % (hid('alpha', 1), WHEN),
                          '%s | %s | pm | selected' % (hid('pm', 1), WHEN)])
        self.assertEqual(self.ok('grep', 'rule', '--repo', 'alpha').splitlines(),
                         ['%s | %s | all | selected' % (rid(1), WHEN),
                          '%s | %s | alpha | selected' % (hid('alpha', 1), WHEN)])
        self.assertEqual(self.run_tool('grep', 'rule', '--repo', 'gamma').returncode, 2)

    def test_list_reads_every_file(self):
        self.assertEqual(self.ok('list', '--prov', 'selected', '--since', '2026-10-06').splitlines(),
                         ['%s | 2026-10-06 | Old rule for all.' % rid(1), '%s | 2026-10-06 | Old rule for beta.' % rid(2),
                          '%s | 2026-10-06 | Alpha rule.' % hid('alpha', 1), '%s | 2026-10-06 | Pm rule.' % hid('pm', 1)])
        self.assertEqual(self.ok('list', '--prov', 'selected', '--since', '2026-10-06', '--repo', 'pm').splitlines(),
                         ['%s | 2026-10-06 | Old rule for all.' % rid(1), '%s | 2026-10-06 | Pm rule.' % hid('pm', 1)])

    def test_check_covers_every_file_and_the_digest(self):
        out = self.ok('check', '--base', 'none')
        self.assertIn('ledger: 4 entries in 3 files, OK', out)
        with open(os.path.join(self.dir, 'STANDING.md'), 'w', encoding='utf-8') as f:
            f.write('# Standing\n\n## alpha\n- %s: Alpha rule.\n- %s: gone\n\n## pm\n- %s: Pm rule.\n'
                    % (hid('alpha', 1), hid('alpha', 7), hid('pm', 1)))
        r = self.run_tool('check', '--base', 'none')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('cites %s, which is not in the ledger' % hid('alpha', 7), r.stdout)
        os.remove(os.path.join(self.dir, 'STANDING.md'))
        with open(self.path('alpha'), 'a', encoding='utf-8') as f:
            f.write('## %s | %s | alpha | selected\nQ: only\n' % (hid('alpha', 5), WHEN))
        r = self.run_tool('check', '--base', 'none')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('FAIL alpha.md: ', r.stdout)

    def test_standing_reads_one_repos_section(self):
        with open(os.path.join(self.dir, 'STANDING.md'), 'w', encoding='utf-8') as f:
            f.write('# Standing\n\n## alpha\n- %s: Alpha rule.\n\n## pm\n- %s: Pm rule.\n'
                    % (hid('alpha', 1), hid('pm', 1)))
        self.assertEqual(self.ok('standing', '--repo', 'alpha').splitlines(),
                         ['## alpha', '- %s: Alpha rule.' % hid('alpha', 1)])
        self.assertEqual(self.ok('standing', 'rule', '--repo', 'pm').splitlines(),
                         ['## pm', '- %s: Pm rule.' % hid('pm', 1)])
        self.assertEqual(len(self.ok('standing', 'rule').splitlines()), 4)
        self.assertEqual(self.run_tool('standing').returncode, 2)


class RetireTest(Base):
    def test_the_frozen_ledger_is_never_written(self):
        r = self.run_tool('retire', rid(1), '--class', 'spent', '--why', 'done')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('frozen', r.stderr)
        self.assertEqual(self.read(self.frozen), frozen_text())

    def test_retire_routes_by_id_and_takes_a_successor_in_another_file(self):
        self.ok(*add_args('alpha'))
        self.ok(*add_args('pm'))
        self.assertEqual(self.ok('retire', hid('alpha', 1), '--class', 'superseded', '--why', 'moved',
                                 '--by', hid('pm', 1)).strip(), hid('alpha', 1))
        self.assertIn('Retired: superseded - moved (see %s)' % hid('pm', 1), self.ok('show', hid('alpha', 1)))

    def test_retire_refuses_an_entry_another_file_cites(self):
        self.ok(*add_args('alpha'))
        self.ok(*add_args('pm', 'Keep %s.' % hid('alpha', 1)))
        r = self.run_tool('retire', hid('alpha', 1), '--class', 'spent', '--why', 'done')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('still cited', r.stderr)


class CommitTest(unittest.TestCase):
    """`add --commit` commits the repo's own file alone, as `ledger: H-<repo>-nnnn ...`."""

    def setUp(self):
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        self.repo.write('rulings/README.md', 'rulings\n')
        self.repo.commit('rulings folder', 'rulings/README.md')
        self.home = os.path.join(self.repo.base, 'home')
        os.makedirs(kuser.config_dir(self.home))
        with open(kuser.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump({'ledger_dir': self.repo.path('rulings'), 'ledger_repos': ['alpha', 'pm']}, f)
        self.env = dict(fixture_env(), USERPROFILE=self.home, HOME=self.home)

    def run_tool(self, *args):
        return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True,
                              env=self.env, timeout=120, cwd=self.repo.root)

    def test_add_commit_commits_the_repos_file_alone(self):
        for repo in ('alpha', 'pm'):
            r = self.run_tool(*add_args(repo, 'Use this.', 'selected', 'chat', '--commit', '--model', 'Test Model 1'))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(r.stdout.splitlines()[0], hid(repo, 1))
            self.assertEqual(self.repo.git('show', '--name-only', '--format=', 'HEAD'), 'rulings/%s.md' % repo)
            self.assertTrue(self.repo.git('log', '-1', '--format=%s').startswith('ledger: %s ' % hid(repo, 1)))
        r = self.run_tool('check', '--base', 'HEAD~2')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('NOT CHECKED', r.stdout)


if __name__ == '__main__':
    unittest.main()
