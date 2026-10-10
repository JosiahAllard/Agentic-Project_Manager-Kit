"""The guard's place reads on a real scratch checkout (docs/guard.md "State"): a main checkout
with a linked worktree nested inside it under .claude/worktrees. A folder of the main checkout is
MAIN, the worktree and its folders are LINKED, and the edit rule follows."""
import json
import os
import subprocess
import unittest
from unittest import mock

from kitlib.git import repo
from kitlib.guard import context, decide
from tests import gitfixture

from .test_fsplace import MINE, OWNER_NOTE
from .world import bash, event


class RealPlaces(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write('.kit.json', json.dumps({'repo': {'name': 'demo'}}))
        self.repo.write('src/a.py', 'a\n')
        self.repo.commit('kit config', '.kit.json', 'src/a.py')
        self.wt = self.repo.worktree('wt')
        os.makedirs(os.path.join(self.wt, 'sub'))
        self.home = os.path.join(self.repo.base, 'home')
        os.makedirs(self.home)

    def test_classify_main_linked_and_nested_in_main(self):
        root = self.repo.root
        for path, kind in ((root, repo.MAIN), (os.path.join(root, 'src'), repo.MAIN),
                           (self.wt, repo.LINKED), (os.path.join(self.wt, 'sub'), repo.LINKED)):
            with self.subTest(path):
                self.assertEqual(repo.classify(path).kind, kind)

    def test_the_edit_rule_on_real_places(self):
        ctx = context.Ctx(environ={}, home=self.home, lock_held=lambda: (False, 'none'))
        in_main = event('Edit', {'file_path': os.path.join(self.repo.root, 'src', 'a.py'), 'old_string': 'a',
                                 'new_string': 'b'}, self.wt, sub=True)
        d = decide(in_main, ctx)
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, 'main-checkout')
        in_wt = event('Edit', {'file_path': os.path.join(self.wt, 'src', 'a.py'), 'old_string': 'a',
                               'new_string': 'b'}, self.wt, sub=True)
        self.assertIsNone(decide(in_wt, ctx))
        self.assertEqual(ctx.main_top(os.path.join(self.wt, 'sub')).replace(chr(92), '/').lower(),
                         self.repo.root.replace(chr(92), '/').lower())

    @unittest.skipUnless(MINE, OWNER_NOTE)
    def test_ordinary_calls_start_no_process(self):
        # KIT-HOOK-SPAWNS: the default Ctx reads places from the filesystem, so a plain command
        # or an edit in a checkout starts no git child (each one is a process launch per call).
        started = []
        real_popen = subprocess.Popen

        def spy(*a, **kw):
            started.append(a[0] if a else kw.get('args'))
            return real_popen(*a, **kw)
        calls = (bash('echo hi', self.wt, sub=True), bash('echo hi', self.repo.root),
                 event('Edit', {'file_path': os.path.join(self.repo.root, 'src', 'a.py'), 'old_string': 'a',
                                'new_string': 'b'}, self.wt, sub=True))
        with mock.patch('subprocess.Popen', spy):
            for ev in calls:
                decide(ev, context.Ctx(environ={}, home=self.home, lock_held=lambda: (False, 'none')))
        self.assertEqual(started, [])


if __name__ == '__main__':
    unittest.main()
