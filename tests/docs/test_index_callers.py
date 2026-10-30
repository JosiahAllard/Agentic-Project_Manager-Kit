"""preflight, commit and review_pack run the doc index check (tools/doc_index.py, default mode)
next to doc_budget, as they once got the index checks through doc_budget: an unlisted .md fails
or is flagged, "doc_index".enabled false skips, a clean tree passes. review_pack reads every doc
index setting at the base (doc_index.py --config-ref), so a branch's .kit.json changes nothing.
A kit with no tools/doc_index.py prints NOT CHECKED (never OK, never failed); a run that does not
finish is FAILED. Each case runs the kit's own tools/ over a real throwaway repository."""
import contextlib
import importlib
import io
import json
import os
import subprocess
import unittest
from unittest import mock

from kitlib import commit, reviewpack
from kitlib.config import load as load_config
from kitlib.docs import callers as doc_callers, index
from kitlib.preflight import report
from tests.docs.test_cli import call, doc_index_cli, repo_with_docs
from tests.gitfixture import fixture_env
from tests.home import no_machine_owner, result_drive, temp_home
from tests.preflight.test_preflight import free_reader
from tools import preflight

# The module, not the function kitlib.config re-exports under the same name.
config_load = importlib.import_module('kitlib.config.load')
KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UNLISTED = 'docs/unlisted.md'


class Case(unittest.TestCase):
    """repo_with_docs() on main (a listed docs/a.md, an index), the kit's real tools."""

    def setUp(self):
        self.repo = repo_with_docs()
        self.addCleanup(self.repo.cleanup)
        self.addCleanup(index.close_readers)
        self.root = self.repo.root
        env = mock.patch.dict(os.environ, fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)

    def scratch_home(self):
        """A home folder with no machine config, so preflight's `config` line never reads the user's."""
        return os.path.join(self.repo.base, 'home')

    def config(self, **sections):
        self.repo.write('.kit.json', json.dumps(dict({'doc_budget': {'budgets': {'docs/a.md': 100}}},
                                                     **sections)))
        self.repo.git('commit', '-q', '-am', 'config')

    def commit_unlisted(self):
        self.repo.write(UNLISTED.replace('/', os.sep), '# Unlisted\n')
        self.repo.git('add', '-A')
        self.repo.git('commit', '-q', '-m', 'add an unlisted doc')


class Preflight(Case):

    def run_preflight(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
                mock.patch.object(preflight, 'KIT', KIT), \
                mock.patch.object(report.stray, 'check', return_value=(0, [])):
            preflight.main(['-C', self.root, '--full'], lock_reader=free_reader, home=self.scratch_home())
        return buf.getvalue()

    def line(self, out):
        [found] = [l for l in out.splitlines() if l.startswith('doc index')]
        return found

    def test_a_clean_tree_passes(self):
        line = self.line(self.run_preflight())
        self.assertNotIn('FAILED', line)
        self.assertIn('DOC INDEX OK', line)

    def test_an_unlisted_doc_fails(self):
        self.commit_unlisted()
        line = self.line(self.run_preflight())
        self.assertIn('FAILED', line)
        self.assertIn('DOC INDEX FAILED', line)

    def test_enabled_false_skips(self):
        self.config(doc_index={'enabled': False})
        self.commit_unlisted()
        out = self.run_preflight()
        self.assertNotIn('doc index', out)

    def test_no_index_is_skipped_not_failed_and_the_line_says_so(self):
        # The doc index is opt-in (decision 26): a repository without one passes, saying why.
        self.repo.git('rm', '-q', 'docs/INDEX.md')
        self.repo.git('commit', '-q', '-m', 'no index')
        self.commit_unlisted()
        line = self.line(self.run_preflight())
        self.assertNotIn('FAILED', line)
        self.assertIn('no docs/INDEX.md in this repository, so the listing check is skipped', line)
        self.assertIn('DOC INDEX OK', line)


class Commit(Case):

    def setUp(self):
        super().setUp()
        # commit's staged machine-path check reads the machine config's machine_path_folders
        temp_home(self)

    def commit(self):
        wt = self.repo.worktree('wt')
        self.repo.write('docs/unlisted.md', '# Unlisted\n', root=wt)
        with mock.patch.object(commit, 'KIT_ROOT', KIT):
            out = commit.commit(commit.request(wt, 'add a doc', ['docs/unlisted.md'],
                                               model='Test Model', cwd=wt))
        return out.code, '\n'.join(out.lines)

    def test_an_unlisted_doc_is_flagged_for_a_follow_up(self):
        code, out = self.commit()
        self.assertEqual(code, 0, out)
        self.assertIn('doc index FAILED', out)
        self.assertIn('DOC INDEX FAILED', out)
        self.assertIn('follow-up commit', out)

    def test_enabled_false_skips(self):
        self.config(doc_index={'enabled': False})
        code, out = self.commit()
        self.assertEqual(code, 0, out)
        self.assertNotIn('doc index', out)
        self.assertNotIn('follow-up commit', out)

    def test_a_clean_tree_passes(self):
        with mock.patch.object(commit, 'KIT_ROOT', KIT):
            line, ok = commit.doc_index_check(self.root)
        self.assertTrue(ok, line)
        self.assertIn('DOC INDEX OK', line)
        self.assertNotIn('FAILED', line)


class ReviewPack(Case):

    def setUp(self):
        super().setUp()
        # no owner email in .kit.json: the owner rules read the machine config's, here none
        no_machine_owner(self)
        result_drive(self)        # the pack's gate line reads the gate-OK record folder

    def pack(self):
        self.repo.git('checkout', '-q', '-b', 'phase/x')
        self.commit_unlisted()
        out = reviewpack.pack(self.root, 'phase/x', base='main')
        return out.code, '\n'.join(out.lines)

    def test_an_unlisted_doc_fails_the_pack(self):
        code, out = self.pack()
        self.assertEqual(code, 1, out)
        [line] = [l for l in out.splitlines() if l.startswith('doc index')]
        self.assertIn('FAILED', line)
        self.assertIn('DOC INDEX FAILED', line)

    def test_enabled_false_in_the_base_skips(self):
        self.config(doc_index={'enabled': False})
        code, out = self.pack()
        self.assertEqual(code, 0, out)
        self.assertNotIn('doc index', out)

    def test_a_branch_cannot_switch_the_index_check_off(self):
        # The base keeps the check on; the branch's own .kit.json turning it off must not count.
        self.repo.git('checkout', '-q', '-b', 'phase/x')
        self.config(doc_index={'enabled': False})
        self.commit_unlisted()
        out = reviewpack.pack(self.root, 'phase/x', base='main')
        # The exit status is not asserted: the branch's .kit.json edit is owner-only and fails
        # the pack on its own. The doc index line is what this case is about.
        [line] = [l for l in out.lines if l.startswith('doc index')]
        self.assertIn('DOC INDEX FAILED', line)
        self.assertIn(UNLISTED, line)

    def test_a_branch_cannot_switch_the_check_back_on_either(self):
        # The base turns it off: the branch's enabled true runs nothing.
        self.config(doc_index={'enabled': False})
        self.repo.git('checkout', '-q', '-b', 'phase/x')
        self.config(doc_index={'enabled': True})
        self.commit_unlisted()
        out = reviewpack.pack(self.root, 'phase/x', base='main')
        self.assertFalse([l for l in out.lines if l.startswith('doc index')], out.lines)

    def test_a_clean_branch_passes(self):
        self.repo.git('checkout', '-q', '-b', 'phase/x')
        self.repo.write('docs/b.md', '# B\n')
        with open(os.path.join(self.root, 'docs', 'INDEX.md'), encoding='utf-8') as fh:
            index = fh.read()
        self.repo.write('docs/INDEX.md', index + '`docs/b.md` | b | reading | kw: beta\n')
        self.repo.git('add', '-A')
        self.repo.git('commit', '-q', '-m', 'b')
        out = reviewpack.pack(self.root, 'phase/x', base='main')
        [line] = [l for l in out.lines if l.startswith('doc index')]
        self.assertNotIn('FAILED', line)
        self.assertIn('DOC INDEX OK', line)


class ConfigRef(Case):
    """kitlib.docs.index.run(config_ref=...): the configuration is the one that revision holds."""

    def check(self, **kw):
        lines = []
        return index.run(self.root, lines.append, **kw), '\n'.join(lines)

    def test_the_ref_s_enabled_counts_not_the_working_tree_s(self):
        base = self.repo.git('rev-parse', 'HEAD').strip()
        self.config(doc_index={'enabled': False})
        self.commit_unlisted()
        code, out = self.check()
        self.assertEqual(code, 0, out)                       # the working tree switches it off
        code, out = self.check(config_ref=base)
        self.assertEqual(code, 1, out)
        self.assertIn('DOC INDEX FAILED', out)
        self.assertIn(UNLISTED, out)

    def test_a_ref_with_no_kit_json_still_checks_an_index_the_head_has(self):
        # The index file alone opts in (decision 26): no .kit.json at the ref is all defaults,
        # "doc_index".enabled true, so the head's index is checked.
        first = self.repo.git('rev-list', '--max-parents=0', 'HEAD').strip()
        self.assertNotEqual(self.repo.git('ls-tree', '--name-only', first, '.kit.json').strip(),
                            '.kit.json')
        self.commit_unlisted()
        for ref in (None, first):
            with self.subTest(config_ref=ref):
                code, out = self.check(config_ref=ref)
                self.assertEqual(code, 1, out)
                self.assertIn('DOC INDEX FAILED', out)
                self.assertIn(UNLISTED, out)

    def test_the_index_path_is_the_ref_s(self):
        base = self.repo.git('rev-parse', 'HEAD').strip()
        self.config(doc_index={'index': 'docs/MAP.md'})       # no such file: skipped if it counted
        code, out = self.check(config_ref=base)
        self.assertEqual(code, 0, out)
        self.assertNotIn('docs/MAP.md', out)
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn('doc_index: no docs/MAP.md in this repository, so the listing check is skipped',
                      out)

    def failing_git(self, rc, words=('show',)):
        """kitlib.config's git seam answering every git call starting with `words` with exit `rc`
        (None: git did not run) and passing every other call through."""
        real = config_load.gitcmd.call

        def call(args, cwd, *a, **k):
            if tuple(args)[:len(words)] == tuple(words):
                return config_load.gitcmd.Result(rc, b'', 'fatal: injected read failure')
            return real(args, cwd, *a, **k)
        return mock.patch.object(config_load.gitcmd, 'call', call)

    def test_a_git_failure_reading_the_ref_refuses_never_skips(self):
        base = self.repo.git('rev-parse', 'HEAD').strip()
        self.commit_unlisted()
        for rc in (128, None):
            with self.subTest(rc=rc), self.failing_git(rc):
                code, out = self.check(config_ref=base)
                self.assertEqual(code, 2, out)
                self.assertIn('doc_index: refused: kit configuration:', out)
                self.assertNotIn('DOC INDEX OK', out)

    def test_no_second_existence_probe_can_skip_the_listing(self):
        # Whether .kit.json exists at the ref comes from the read the settings came from: a
        # separate `git cat-file -e` that failed once read as "no .kit.json" and skipped the check.
        base = self.repo.git('rev-parse', 'HEAD').strip()
        self.commit_unlisted()
        for rc in (128, None):
            with self.subTest(rc=rc), self.failing_git(rc, ('cat-file', '-e')):
                code, out = self.check(config_ref=base)
                self.assertEqual(code, 1, out)
                self.assertIn('DOC INDEX FAILED', out)

    def test_an_unknown_ref_refuses(self):
        code, out = self.check(config_ref='no-such-ref')
        self.assertEqual(code, 2, out)
        self.assertIn('doc_index: refused: kit configuration:', out)
        self.assertNotIn('DOC INDEX OK', out)

    def test_the_cli_passes_config_ref(self):
        base = self.repo.git('rev-parse', 'HEAD').strip()
        self.config(doc_index={'enabled': False})
        self.commit_unlisted()
        code, out, _ = call(doc_index_cli.main, '-C', self.root, '--config-ref', base)
        self.assertEqual(code, 1, out)
        self.assertIn('DOC INDEX FAILED', out)


def timing_out(tool_name, real):
    """A stand-in for subprocess.run that times out a run of `tool_name` and passes the rest."""
    def run(argv, *a, **k):
        if any(str(x).endswith(tool_name) for x in argv):
            raise subprocess.TimeoutExpired(argv, k.get('timeout'))
        return real(argv, *a, **k)
    return run


class NotCheckedAndTimeout(Case):
    """The doc index line when the kit has no tools/doc_index.py (NOT CHECKED, never green and
    never failed), and when the tool does not finish (FAILED, never green)."""

    def setUp(self):
        super().setUp()
        self.empty_kit = os.path.join(self.repo.base, 'no-kit')
        os.makedirs(os.path.join(self.empty_kit, 'tools'), exist_ok=True)

    def test_review_pack_without_the_tool_is_not_checked(self):
        with mock.patch.object(reviewpack, 'DOC_INDEX',
                               os.path.join(self.empty_kit, 'tools', 'doc_index.py')):
            line, failed = reviewpack.index_line(self.root, 'main', 'main',
                                                 load_config(self.root))
        self.assertFalse(failed)
        self.assertIn('NOT CHECKED', line)
        self.assertNotIn('OK', line)

    def test_review_pack_timeout_fails(self):
        with mock.patch.object(reviewpack.subprocess, 'run',
                               timing_out('doc_index.py', subprocess.run)):
            line, failed = reviewpack.index_line(self.root, 'main', 'main',
                                                 load_config(self.root))
        self.assertTrue(failed)
        self.assertTrue(line.startswith('doc index FAILED'), line)
        self.assertIn('did not finish in %d seconds' % doc_callers.TIMEOUT, line)

    def test_commit_without_the_tool_is_not_checked(self):
        line, ok = commit.doc_index_check(self.root, kit_root=self.empty_kit)
        self.assertTrue(ok)
        self.assertIn('NOT CHECKED', line)
        self.assertNotIn('OK', line)

    def test_commit_timeout_fails(self):
        with mock.patch.object(commit.subprocess, 'run',
                               timing_out('doc_index.py', subprocess.run)):
            line, ok = commit.doc_index_check(self.root, kit_root=KIT)
        self.assertFalse(ok)
        self.assertIn('doc index FAILED', line)
        self.assertIn('did not finish', line)

    def preflight_line(self, kit, machine_run=None):
        buf = io.StringIO()
        patches = [mock.patch.object(preflight, 'KIT', kit),
                   mock.patch.object(report.stray, 'check', return_value=(0, []))]
        if machine_run is not None:
            patches.append(mock.patch.object(report.machine, 'run', machine_run))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stdout(buf))
            preflight.main(['-C', self.root, '--full'], lock_reader=free_reader, home=self.scratch_home())
        [found] = [l for l in buf.getvalue().splitlines() if l.startswith('doc index')]
        return found

    def test_preflight_without_the_tool_is_not_checked(self):
        line = self.preflight_line(self.empty_kit)
        self.assertIn('NOT CHECKED', line)
        self.assertNotIn('OK', line)

    def test_preflight_timeout_fails(self):
        real = report.machine.run

        def run(argv, timeout=None):
            if any(str(x).endswith('doc_index.py') for x in argv):
                return 1, '', str(subprocess.TimeoutExpired(argv, timeout))
            return real(argv, timeout=timeout) if timeout is not None else real(argv)
        line = self.preflight_line(KIT, run)
        self.assertTrue(line.startswith('doc index FAILED'), line)
        self.assertIn('timed out', line)


if __name__ == '__main__':
    unittest.main()
