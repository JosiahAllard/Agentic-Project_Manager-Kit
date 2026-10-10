"""preflight, commit and review_pack run the doc index check (tools/doc_index.py, default mode)
next to doc_budget, as they once got the index checks through doc_budget: an unlisted .md fails
or is flagged, "doc_index".enabled false skips, a clean tree passes. Each case runs the kit's own
tools/ over a real throwaway repository."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from kitlib import commit, reviewpack
from kitlib.preflight import report
from tests.docs.test_cli import repo_with_docs
from tests.gitfixture import fixture_env
from tests.preflight.test_preflight import free_reader
from tools import preflight

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UNLISTED = 'docs/unlisted.md'


class Case(unittest.TestCase):
    """repo_with_docs() on main (a listed docs/a.md, an index), the kit's real tools."""

    def setUp(self):
        self.repo = repo_with_docs()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        env = mock.patch.dict(os.environ, fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)

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
            preflight.main(['-C', self.root], lock_reader=free_reader)
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


class Commit(Case):

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
        text = '\n'.join(out.lines)
        self.assertEqual(out.code, 1, text)
        [line] = [l for l in out.lines if l.startswith('doc index')]
        self.assertIn('DOC INDEX FAILED', line)

    def test_a_branch_cannot_switch_the_check_back_on_either(self):
        # The base turns it off: the branch's enabled true runs nothing.
        self.config(doc_index={'enabled': False})
        self.repo.git('checkout', '-q', '-b', 'phase/x')
        self.config(doc_index={'enabled': True})
        self.commit_unlisted()
        out = reviewpack.pack(self.root, 'phase/x', base='main')
        text = '\n'.join(out.lines)
        self.assertEqual(out.code, 0, text)
        self.assertNotIn('doc index', text)

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


if __name__ == '__main__':
    unittest.main()
