"""The entry points tools/doc_budget.py, tools/doc_index.py and tools/codemap.py: -C, USAGE exits
and the verdict line other tools judge (docs/doc-tools.md). The checks themselves are tested
through the library in test_budget.py, test_index.py and test_codemap.py."""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from kitlib import verdict
from tests.gitfixture import TempRepo, fixture_env

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT, 'tools'))

import codemap as codemap_cli                                     # noqa: E402
import doc_budget as doc_budget_cli                               # noqa: E402
import doc_index as doc_index_cli                                 # noqa: E402

INDEX = '`docs/INDEX.md` | index | always | kw: index\n`docs/a.md` | a doc | reading it | kw: alpha\n'


def repo_with_docs():
    r = TempRepo()
    r.write('.kit.json', json.dumps({'doc_budget': {'budgets': {'docs/a.md': 100}}}))
    r.write('docs/INDEX.md', INDEX)
    r.write('docs/a.md', '# A\n\ntext\n')
    r.git('rm', '-q', 'README.md')
    r.git('add', '-A')
    r.git('commit', '-q', '-m', 'docs')
    return r


def call(main, *argv):
    """(exit status, stdout, stderr) of an entry point's main()."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class DocBudgetAsOtherToolsRunIt(unittest.TestCase):
    """commit.py, review_pack.py and preflight.py run `[sys.executable, tools/doc_budget.py, -C,
    <repo>]` and judge its output with kitlib.verdict.judge."""

    def setUp(self):
        self.repo = repo_with_docs()
        self.addCleanup(self.repo.cleanup)

    def run_tool(self):
        p = subprocess.run([sys.executable, os.path.join(KIT, 'tools', 'doc_budget.py'), '-C', self.repo.root],
                           cwd=self.repo.base, capture_output=True, text=True, env=fixture_env(),
                           timeout=300)
        return p.returncode, p.stdout + p.stderr

    def test_a_clean_repo_is_judged_ok(self):
        code, text = self.run_tool()
        self.assertEqual(code, 0, text)
        self.assertEqual(verdict.judge(code, text), (True, ''), text)
        self.assertTrue(text.splitlines()[-1].startswith('DOC BUDGET OK ('), text)

    def test_an_over_budget_doc_is_judged_failed(self):
        self.repo.git('checkout', '-q', '-b', 'phase/x')         # grown on a branch, not on main
        self.repo.write('docs/a.md', 'x' * 200)
        self.repo.git('commit', '-q', '-am', 'grow')
        code, text = self.run_tool()
        self.assertEqual(code, 1, text)
        passed, why = verdict.judge(code, text)
        self.assertFalse(passed, text)
        self.assertIn('DOC BUDGET FAILED', text)

    def test_a_folder_outside_any_repository_refuses_with_2(self):
        code, out, _ = call(doc_budget_cli.main, '-C', self.repo.base)
        self.assertEqual(code, 2, out)
        self.assertIn('doc_budget: refused: -C', out)
        self.assertFalse(verdict.judge(code, out)[0])

    def test_a_usage_error_prints_usage_and_exits_2(self):
        code, _, err = call(doc_budget_cli.main, '--no-such-option')
        self.assertEqual(code, 2)
        self.assertIn('USAGE: unrecognized arguments: --no-such-option', err)


class DocIndexCli(unittest.TestCase):
    def setUp(self):
        self.repo = repo_with_docs()
        self.addCleanup(self.repo.cleanup)

    def test_find_joins_its_words_and_prints_the_entry(self):
        code, out, _ = call(doc_index_cli.main, '-C', self.repo.root, '--find', 'alpha')
        self.assertEqual(code, 0, out)
        self.assertIn('docs/a.md', out)

    def test_the_modes_are_mutually_exclusive(self):
        code, _, err = call(doc_index_cli.main, '--tools', '--find', 'x')
        self.assertEqual(code, 2)
        self.assertIn('USAGE: argument --find: not allowed with argument --tools', err)

    def test_a_missing_index_says_there_is_none_and_where_to_search(self):
        # The doc index is opt-in: a repository without one is told where to look, not refused.
        os.remove(os.path.join(self.repo.root, 'docs', 'INDEX.md'))
        for mode in (('--find', 'alpha'), ('--tools',)):
            with self.subTest(mode=mode):
                code, out, _ = call(doc_index_cli.main, '-C', self.repo.root, *mode)
                self.assertEqual(code, 1, out)
                self.assertIn('doc_index: this repository has no doc index (no docs/INDEX.md)', out)
                self.assertIn('git grep -n -i "<topic>" -- docs README.md', out)
                self.assertNotIn('cannot read', out)

    def test_a_broken_kit_json_refuses(self):
        self.repo.write('.kit.json', '{broken')
        code, out, _ = call(doc_index_cli.main, '-C', self.repo.root, '--find', 'alpha')
        self.assertEqual(code, 2, out)
        self.assertIn('.kit.json', out)


class CodemapCli(unittest.TestCase):
    def test_file_mode_through_dash_c(self):
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        repo.write('tools/x.py', 'def fresh():\n    pass\n')
        code, out, _ = call(codemap_cli.main, '-C', repo.root, '--file', 'tools/x.py')
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), ['tools/x.py  2 lines', '      1    fresh()'])

    def test_a_usage_error_exits_2(self):
        code, _, err = call(codemap_cli.main, '--find')
        self.assertEqual(code, 2)
        self.assertIn('USAGE: argument --find: expected one argument', err)


if __name__ == '__main__':
    unittest.main()
