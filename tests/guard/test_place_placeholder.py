"""The `<main checkout>` placeholder an alternative names where git cannot place the checkout is
text, never a path (rules.new_worktree): the rules ask the place of absolute paths only, so no
decision reads this process's folder. Read as a path, the placeholder was placed against the
folder the hook runs in, and from the PM data worktree a subagent's commit in an unplaced
checkout was told it worked in the PM data worktree "<main checkout>"."""
import os
import unittest

from . import world as W
from .claudehost import decide
from .test_rules import ROWS
from .world import bash

WT_ADD = 'git -C "<main checkout>" worktree add .claude/worktrees/<slug> -b <branch>'

setUpModule = W.deny_softened           # test_rules.py's rows, judged as it judges them
tearDownModule = W.allow_unplaced


class Placeholder(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.w.git.ancestors.add((W.SHA_WT, 'main'))

    def test_every_row_places_absolute_paths_only(self):
        for name, make, _kind, _rule, _instead in ROWS:
            seen = []

            def record(path, seen=seen):
                seen.append(path)
                return self.w.classify(path)
            decide(make(self.w), self.w.ctx(classify=record))
            with self.subTest(name):
                self.assertEqual([p for p in seen if not os.path.isabs(p)], [])

    def test_the_folder_the_hook_runs_in_changes_no_decision(self):
        self.w.user_config({'pm_data': {'path': self.w.data}})
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        for text, cwd in (('git commit -m x', self.w.unknown), ('git -C $x commit -m x', self.w.wt)):
            got = []
            for folder in (self.w.base, self.w.data):
                os.chdir(folder)
                d = decide(bash(text, cwd, True), self.w.ctx())
                got.append((d.verdict, d.rule, d.reason))
                with self.subTest(text, folder=folder):
                    self.assertEqual((d.verdict, d.rule, d.instead), ('deny', 'unplaced', WT_ADD), d.reason)
                    self.assertNotIn('PM data worktree', d.reason)
            os.chdir(here)
            with self.subTest(text, same=True):
                self.assertEqual(got[0], got[1])


if __name__ == '__main__':
    unittest.main()
