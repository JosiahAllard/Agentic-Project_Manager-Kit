"""The kit tools that run the doc tools, called the way they call them: board (kitlib.board.report
docs_line: doc_index.py --stale -C), commit (kitlib.commit doc_budget_check: doc_budget.py -C) and
review_pack (kitlib.reviewpack docs_line: doc_budget.py -C --base --head). Each must reach a real
verdict from this kit's tools/, never NOT CHECKED, a USAGE error or a refusal."""
import os
import unittest
from unittest import mock

from kitlib import commit, reviewpack
from kitlib.board import report as board
from tests.docs.test_cli import repo_with_docs
from tests.gitfixture import fixture_env

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class CallersReachTheDocTools(unittest.TestCase):

    def setUp(self):
        self.repo = repo_with_docs()
        self.addCleanup(self.repo.cleanup)
        env = mock.patch.dict(os.environ, fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)

    def assertReached(self, line):
        for bad in ('NOT CHECKED', 'USAGE', 'refused', 'unrecognized arguments'):
            self.assertNotIn(bad, line)

    def test_board_stale_report(self):
        line = board.docs_line(self.repo.root, KIT)
        self.assertReached(line)
        self.assertTrue(line.startswith('docs: STALE REPORT'), line)

    def test_commit_doc_budget_check(self):
        line, ok = commit.doc_budget_check(self.repo.root, KIT)
        self.assertReached(line)
        self.assertTrue(ok, line)

    def test_review_pack_docs_line(self):
        self.repo.git('checkout', '-q', '-b', 'phase/x')
        line, failed = reviewpack.docs_line(self.repo.root, 'main', 'phase/x')
        self.assertReached(line)
        self.assertFalse(failed, line)


if __name__ == '__main__':
    unittest.main()
