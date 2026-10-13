"""kitlib.rulingcites and tools/ruling_cites.py (docs/repo-checks.md): a branch fails only for a
citation it ADDS of a ruling the ledger cannot back (PM R-9570); the same citation on an untouched
line warns. Every case gets its own git repository (a copy of one built once per process), its
own ledger and its own machine config in a temp home - the real ledger and the real machine
config are never read."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import rulingcites as RC
from kitlib.git import batch as gitbatch
from kitlib.git import cmd as gitcmd
from tests.gitfixture import EMAIL, NAME, built_copy, fixture_env

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import ruling_cites as cli  # noqa: E402
from kitlib.config import user as kuser  # noqa: E402

LEDGER = ('# Rulings ledger (test fixture)\n\n'
          '## R-9001 | 2026-09-25 08:00Z | beta | typed\nQ: q\nA: a\n\n'
          '## R-9002 | 2026-09-25 08:01Z | all | selected\nRetired: superseded - by one (see R-9001)\n\n'
          '## R-9003 | 2026-09-25 08:02Z | gamma | typed\nQ: q\nA: a\n\n'
          '## R-9004 | 2026-09-25 08:03Z | beta | typed\nRetired: spent - done (see -)\n\n'
          '## R-9005 | 2026-09-25 08:04Z | all | typed\nRetired: duplicate - of two (see R-9002)\n\n'
          '## R-9006 | 2026-09-25 08:05Z | pm | typed\nQ: q\nA: a\n')


class Fixture(unittest.TestCase):
    """A git repository in a folder named FOLDER on branch MAIN, with `topic` checked out; a ledger
    and a temp home whose machine config names it. No tests of its own."""

    FOLDER = 'beta'
    MAIN = 'main'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.root = os.path.join(tmp.name, self.FOLDER)
        os.makedirs(self.root)
        self.ledger_path = os.path.join(tmp.name, 'LEDGER.md')
        with open(self.ledger_path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(LEDGER)
        self.home = os.path.join(tmp.name, 'home')
        self.machine_config({'ledger_path': self.ledger_path})
        env = mock.patch.dict(os.environ, fixture_env({'USERPROFILE': self.home, 'HOME': self.home}),
                              clear=True)
        env.start()
        self.addCleanup(env.stop)
        # Built once per process per main branch name, then copied (tests/gitfixture.py).
        built_copy(('test_ruling_cites.Fixture', self.MAIN), self.root, self.build_repo)

    def build_repo(self, root):
        def git(*args):
            gitcmd.subprocess_run(('git', '-C', root) + args, check=True, capture_output=True,
                                  env=fixture_env())
        git('init', '-q', '-b', self.MAIN)
        for k, v in (('user.name', NAME), ('user.email', EMAIL), ('core.autocrlf', 'false'),
                     ('commit.gpgsign', 'false')):
            git('config', k, v)
        path = os.path.join(root, 'docs', 'a.md')
        os.makedirs(os.path.dirname(path))
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('old line citing PM R-9002\nfine PM R-9001\n')
        git('add', '-A')
        git('commit', '-q', '-m', 'base')
        git('switch', '-q', '-c', 'topic')

    def machine_config(self, data):
        folder = kuser.config_dir(self.home)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'config.json'), 'w', encoding='utf-8') as fh:
            json.dump(data, fh)

    def git(self, *args):
        gitcmd.subprocess_run(('git', '-C', self.root) + args, check=True, capture_output=True,
                              env=fixture_env())

    def write(self, rel, text):
        path = os.path.join(self.root, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)

    def commit(self, msg):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', msg)

    def set_config(self, **sections):
        """Write `.kit.json` and commit it, so it is neither untracked nor an added line."""
        self.write('.kit.json', json.dumps(sections))
        self.commit('config')

    def run_tool(self, *extra, base=True):
        """`base`: True passes `--base MAIN`, None passes no `--base`, else that ref."""
        base = self.MAIN if base is True else base
        args = ['-C', self.root] + (['--base', base] if base else []) + list(extra)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(args)
        return code, out.getvalue()


class RulingCites(Fixture):
    """The fixture folder is `beta`: with no `.kit.json`, its ledger name is its folder
    name."""

    def test_an_untouched_retired_citation_warns_and_passes(self):
        code, out = self.run_tool()
        self.assertEqual(code, 0, out)
        self.assertIn('WARNING: docs/a.md:1: `PM R-9002` is retired', out)
        self.assertIn('cite R-9001 instead', out)
        self.assertIn('RULING CITES OK', out)

    def test_an_added_retired_citation_fails_and_names_its_successor(self):
        self.write('docs/b.md', 'new text, PM R-9005\n')
        self.commit('add')
        code, out = self.run_tool()
        self.assertEqual(code, 1, out)
        # R-9005 -> R-9002 -> R-9001: the chain is followed to the live entry.
        self.assertIn('docs/b.md:1: `PM R-9005` is retired in the PM\'s rulings ledger (duplicate): '
                      'cite R-9001 instead', out)
        self.assertIn('RULING CITES FAILED', out)

    def test_an_edit_that_adds_the_citation_to_an_existing_file_fails(self):
        self.write('docs/a.md', 'old line citing PM R-9002\nfine PM R-9001\nadded PM R-9004\n')
        code, out = self.run_tool()                     # uncommitted: the working tree counts
        self.assertEqual(code, 1, out)
        self.assertIn('docs/a.md:3: `PM R-9004`', out)
        self.assertIn('drop the citation', out)
        self.assertIn('WARNING: docs/a.md:1:', out)     # the untouched line still only WARNs

    def test_an_untracked_file_counts_as_added(self):
        self.write('docs/new.md', 'PM R-9099\n')
        code, out = self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn('`PM R-9099` is not a ruling header', out)

    def test_a_foreign_ruling_fails(self):
        self.write('docs/b.md', 'PM R-9003\n')
        code, out = self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn('is a ruling for `gamma`, not for beta or all', out)

    def test_every_id_in_a_run_is_read(self):
        self.write('docs/b.md', 'per PM R-9001/R-9004 and more\nPM R-9001, R-9099\n')
        code, out = self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn('docs/b.md:1: `PM R-9004`', out)
        self.assertIn('docs/b.md:2: `PM R-9099`', out)

    def test_a_bare_id_and_test_data_are_not_read(self):
        self.write('docs/b.md', 'priority_ruling: R-9004 is a record field, not prose\n')
        self.write('tools/test_x.py', "LEDGER = 'PM R-9004'\n")
        self.write('tools/fixtures/f.txt', 'PM R-9004\n')
        code, out = self.run_tool()
        self.assertEqual(code, 0, out)

    def test_an_unreadable_ledger_warns_and_checks_nothing(self):
        os.remove(self.ledger_path)
        self.write('docs/b.md', 'PM R-9099\n')
        code, out = self.run_tool()
        self.assertEqual(code, 0, out)
        self.assertIn('WARNING: ruling citations NOT CHECKED - the rulings ledger is unreadable', out)
        self.assertNotIn('RULING CITES OK', out)

    def test_no_configured_ledger_warns_and_checks_nothing(self):
        self.machine_config({})
        self.write('docs/b.md', 'PM R-9099\n')
        code, out = self.run_tool()
        self.assertEqual(code, 0, out)
        self.assertIn('WARNING: ruling citations NOT CHECKED - no rulings ledger is configured', out)
        self.assertNotIn('RULING CITES OK', out)

    def test_a_bad_machine_config_refuses(self):
        self.machine_config({'ledger_path': 'relative/LEDGER.md'})
        code, out = self.run_tool()
        self.assertEqual(code, 2, out)
        self.assertIn('ruling_cites: refused: kit configuration:', out)
        self.assertIn('ledger_path', out)

    def test_the_ledger_is_the_machine_configs(self):
        other = os.path.join(self.tmp, 'OTHER.md')
        with open(other, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(LEDGER.replace('| gamma |', '| beta |'))
        self.machine_config({'ledger_path': other})
        self.write('docs/b.md', 'PM R-9003\n')
        code, out = self.run_tool()
        self.assertEqual(code, 0, out)
        self.assertIn('RULING CITES OK', out)

    def test_a_base_git_cannot_resolve_is_exit_2(self):
        code, out = self.run_tool(base='no-such-ref')
        self.assertEqual(code, 2, out)
        self.assertIn('RULING CITES NOT CHECKED', out)

    def test_it_names_no_ledger_path(self):
        # The ledger path has one place: the machine config (R-9343, R-9368).
        for rel in ('kitlib/rulingcites/__init__.py', 'tools/ruling_cites.py'):
            with open(os.path.join(KIT_ROOT, *rel.split('/')), encoding='utf-8') as fh:
                text = fh.read()
            self.assertNotIn('LEDGER.md', text, rel)
            self.assertNotIn('os.environ', text, rel)

    def test_an_added_line_starting_with_plus_plus_stays_in_its_file(self):
        # `+++ ` inside a hunk is content: it must not reset the file and turn FAIL into WARN.
        self.write('docs/b.md', '++ note PM R-9004\nPM R-9099\n')
        self.commit('add')
        code, out = self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn('docs/b.md:1: `PM R-9004`', out)
        self.assertIn('docs/b.md:2: `PM R-9099`', out)
        self.assertNotIn('WARNING: docs/b.md', out)

    def test_diff_prefix_and_quoting_config_do_not_downgrade_a_fail(self):
        for i, key in enumerate(('diff.noprefix', 'diff.mnemonicPrefix', 'core.quotePath')):
            with self.subTest(key=key):
                self.git('config', key, 'true')
                rel = 'docs/naïve %d b.md' % i  # non-ASCII and a space: git quotes/tabs it
                self.write(rel, 'PM R-9099\n')
                code, out = self.run_tool()
                self.assertEqual(code, 1, out)
                self.assertIn('%s:1: `PM R-9099` is not a ruling header' % rel, out)
                self.commit('commit %d' % i)            # committed: the diff path, not untracked
                code, out = self.run_tool()
                self.assertEqual(code, 1, out)
                self.assertIn('%s:1: `PM R-9099`' % rel, out)
                self.assertNotIn('WARNING: docs/na', out)
                self.git('config', '--unset', key)

    def test_unquote_path_reads_git_c_quoting(self):
        self.assertEqual(gitbatch.unquote_path('"b/na\\303\\257ve.md"'), 'b/naïve.md')
        self.assertEqual(gitbatch.unquote_path('"b/a\\"q\\\\.md"'), 'b/a"q\\.md')
        self.assertEqual(gitbatch.unquote_path('b/plain.md'), 'b/plain.md')


class Library(Fixture):
    """check() takes the ledger as an argument, so a caller (or a test) need not touch config."""

    def test_an_explicit_ledger_wins_over_the_machine_config(self):
        self.machine_config({})
        self.write('docs/b.md', 'PM R-9099\n')
        out = RC.check(self.root, base=self.MAIN, ledger=self.ledger_path)
        self.assertEqual(out.code, RC.FAILED, out.lines)
        self.assertIn('docs/b.md:1: `PM R-9099` is not a ruling header in the PM\'s rulings ledger',
                      out.lines)


class TreeFiles(Fixture):
    """Tracked and untracked come from one `ls-files -t -co` git process."""

    def test_tracked_untracked_and_ignored_files_in_one_git_process(self):
        self.write('.gitignore', '*.log\n')
        self.write('kept.log', 'tracked although ignored\n')
        self.git('add', '-f', '.gitignore', 'kept.log')
        self.write('new file.txt', 'untracked, with a space\n')
        self.write('skip.log', 'untracked and ignored\n')
        calls = []
        real = gitcmd.call

        def spy(args, cwd, **kw):
            calls.append(list(args))
            return real(args, cwd, **kw)
        with mock.patch.object(gitcmd, 'call', spy):
            tracked, untracked = RC.tree_files(self.root)
        self.assertEqual(sorted(tracked), ['.gitignore', 'docs/a.md', 'kept.log'])
        self.assertEqual(untracked, {'new file.txt'})
        self.assertEqual(len(calls), 1, calls)


class TheLedgerNameComesFromConfig(RulingCites):
    """`.kit.json` "ruling_cites".repo: the PM checkout's folder is not its ledger name (`pm`).
    Every RulingCites case runs again with the folder renamed and the name configured."""

    FOLDER = 'pm-repo-checkout'

    def setUp(self):
        super().setUp()
        self.set_config(ruling_cites={'repo': 'Beta'})   # lower-cased when read


class TheConfiguredNameIsTheOneJudged(Fixture):
    """`.kit.json` "ruling_cites".repo names the ledger repo, whatever the folder is called."""

    FOLDER = 'pm-repo-checkout'

    def test_the_configured_name_is_the_one_judged(self):
        self.set_config(ruling_cites={'repo': 'pm'})
        self.write('docs/b.md', 'PM R-9006\nPM R-9001\n')
        code, out = self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertNotIn('`PM R-9006`', out)
        self.assertIn('docs/b.md:2: `PM R-9001` is a ruling for `beta`, not for pm or all',
                      out)


class TheDefaultLedgerNameIsTheRepoName(Fixture):
    """No "ruling_cites" section: `.kit.json` "repo".name, else the folder, lower-cased."""

    FOLDER = 'some-folder'

    def test_the_repo_name_wins_over_the_folder(self):
        self.set_config(repo={'name': 'Gamma'})
        self.write('docs/b.md', 'PM R-9003\nPM R-9001\n')
        code, out = self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertNotIn('`PM R-9003`', out)
        self.assertIn('`PM R-9001` is a ruling for `beta`, not for gamma or all', out)

    def test_the_folder_name_without_config(self):
        self.write('docs/b.md', 'PM R-9001\n')
        code, out = self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn('not for some-folder or all', out)

    def test_the_name_read_from_the_remote_is_not_the_ledger_name(self):
        # "repo".name unset reads the remote's URL (docs/kit-json.md "With no file"); the ledger
        # column takes a written name only, as kitlib.backlog.ledger.ledger_name does
        gitcmd.subprocess_run(('git', '-C', self.root, 'remote', 'add', 'origin', 'https://example.com/x/beta.git'),
                              check=True, capture_output=True, env=fixture_env())
        self.assertEqual(RC.settings(self.root)[0], ('some-folder', 'all'))
        self.set_config(repo={'name': 'Gamma'})
        self.assertEqual(RC.settings(self.root)[0], ('gamma', 'all'))


class TheSkipGlobs(Fixture):

    def test_the_default_skip_follows_the_tools_dir(self):
        self.set_config(repo={'tools_dir': 'scripts'})
        self.write('scripts/test_x.py', "LEDGER = 'PM R-9004'\n")
        self.write('scripts/fixtures/f.txt', 'PM R-9004\n')
        self.write('tools/test_x.py', "LEDGER = 'PM R-9004'\n")
        code, out = self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn('tools/test_x.py:1: `PM R-9004`', out)
        self.assertNotIn('scripts/', out)

    def test_configured_globs_replace_the_default(self):
        self.set_config(ruling_cites={'skip': ['docs/old/*']})
        self.write('docs/old/x.md', 'PM R-9004\n')
        self.write('tools/test_x.py', "LEDGER = 'PM R-9004'\n")
        code, out = self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn('tools/test_x.py:1:', out)
        self.assertNotIn('docs/old/', out)

    def test_an_empty_list_skips_nothing(self):
        self.set_config(ruling_cites={'skip': []})
        self.write('tools/test_x.py', "LEDGER = 'PM R-9004'\n")
        code, out = self.run_tool()
        self.assertEqual(code, 1, out)
        self.assertIn('tools/test_x.py:1:', out)


class TheDefaultBaseIsTheMainBranch(Fixture):
    """No `--base`: `.kit.json` "repo".main_branch, else `main`."""

    MAIN = 'master'

    def test_the_configured_main_branch(self):
        self.set_config(repo={'main_branch': 'master'})
        self.write('docs/b.md', 'PM R-9099\n')
        self.commit('add')
        code, out = self.run_tool(base=None)
        self.assertEqual(code, 1, out)
        self.assertIn('docs/b.md:1: `PM R-9099`', out)

    def test_without_config_the_base_is_main(self):
        code, out = self.run_tool(base=None)          # this repository has no `main`
        self.assertEqual(code, 2, out)
        self.assertIn('against main', out)


class TheConfigRefusesWhatItCannotUse(Fixture):

    def refused(self, data):
        self.write('.kit.json', json.dumps(data))
        code, out = self.run_tool()
        self.assertEqual(code, 2, out)
        self.assertIn('ruling_cites: refused: kit configuration:', out)
        return out

    def test_an_unknown_key(self):
        self.assertIn('ledger', self.refused({'ruling_cites': {'ledger': 'x'}}))

    def test_a_repo_that_is_not_a_name(self):
        self.assertIn('repo', self.refused({'ruling_cites': {'repo': ''}}))
        self.assertIn('repo', self.refused({'ruling_cites': {'repo': 3}}))

    def test_skip_that_is_not_a_list_of_globs(self):
        self.assertIn('skip', self.refused({'ruling_cites': {'skip': 'tools/*'}}))
        self.assertIn('skip', self.refused({'ruling_cites': {'skip': ['']}}))

    def test_outside_a_repository_is_two(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(['-C', d]), 2)
        self.assertIn('NOT CHECKED', out.getvalue())

    def test_usage_is_two(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                cli.main(['--root', self.root])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('USAGE:', err.getvalue())


if __name__ == '__main__':
    unittest.main()
