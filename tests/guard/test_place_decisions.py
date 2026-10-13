"""Reading places from the filesystem (kitlib.guard.place) changes no guard decision: every row
of the rule table (tests/guard/test_rules.py ROWS, root searches and main-checkout edits
included) is decided twice on a real repository laid out as world.World lays out its fake one,
once with git's classify and once with the filesystem's, and the two decisions must be the same
(kind, rule and the full reason). The filesystem side must also have answered every place
itself: a run that quietly fell back to git would prove nothing."""
import os
import shutil
import subprocess
import unittest

from kitlib.git import repo
from kitlib.guard import place
from tests import gitfixture
from tests.realgit import real_git

from . import world as W
from .claudehost import decide
from .test_fsplace import MINE
from .test_rules import ROWS


class RealWorld(W.World):
    """world.World with base/repo a real main checkout and .claude/worktrees/wt its real linked
    worktree; the fake refs, gate records and lock stay as they are."""

    def __init__(self):
        W.World.__init__(self)
        env = gitfixture.fixture_env()

        def git(*args, cwd=None):
            subprocess.run(('git',) + args, cwd=cwd or self.main, env=env, check=True, capture_output=True)
        git('init', '-q', '-b', 'main')
        for k, v in (('user.name', gitfixture.NAME), ('user.email', gitfixture.EMAIL),
                     ('commit.gpgsign', 'false')):
            git('config', k, v)
        git('add', '.kit.json')
        git('commit', '-q', '-m', 'kit config')
        os.rmdir(self.wt)
        git('worktree', 'add', '-q', self.wt, '-b', 'kit/wt')
        os.makedirs(os.path.join(self.wt, 'src'), exist_ok=True)


class Memo(object):
    def __init__(self, fn):
        self.fn, self.seen, self.calls = fn, {}, 0

    def __call__(self, path):
        self.calls += 1
        key = os.path.normcase(os.path.abspath(path))
        if key not in self.seen:
            self.seen[key] = self.fn(path)
        return self.seen[key]


@real_git("checks the filesystem's places against git's own rev-parse")
class SameDecisions(unittest.TestCase):
    def setUp(self):
        self.w = RealWorld()
        self.addCleanup(lambda: shutil.rmtree(self.w.base, True) if os.path.isdir(self.w.base) else None)
        self.w.git.ancestors.add((W.SHA_WT, 'main'))

    def test_every_row_decides_the_same(self):
        by_git = Memo(repo.classify)
        fell_back = Memo(repo.classify)
        by_fs = lambda path: place.classify(path, fallback=fell_back)   # noqa: E731
        rules_seen = set()
        for name, make, _kind, _rule, _instead in ROWS:
            with self.subTest(name):
                a = decide(make(self.w), self.w.ctx(classify=by_git))
                b = decide(make(self.w), self.w.ctx(classify=by_fs))
                want = a and (a.verdict, a.rule, a.reason)
                self.assertEqual(b and (b.verdict, b.rule, b.reason), want)
                if a:
                    rules_seen.add(a.rule)
        if MINE:     # an elevated run's files are Administrators': the owner check hands them to git
            self.assertEqual(fell_back.calls, 0,
                             'the filesystem reader handed %d place(s) to git' % fell_back.calls)
        self.assertGreater(by_git.calls, 0)
        for rule in ('root-search', 'main-checkout', 'heavy', 'git-checkout'):
            self.assertIn(rule, rules_seen)


if __name__ == '__main__':
    unittest.main()
