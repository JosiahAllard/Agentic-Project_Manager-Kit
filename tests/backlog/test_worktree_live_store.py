"""The store a linked worktree opens (docs/backlog.md "The store a worktree opens";
kitlib.backlog.store.worktree_live_store, opener.open_store): a worktree whose `.kit.json` names
no "backlog".store, in a repository whose PM data worktree names one, reads and writes that live
store, acting as its own checked-out branch. `-C <worktree>` then does what
`-C <pm data> --as <its branch>` does. The main checkout, the PM data worktree itself, a worktree
naming its own store and a detached HEAD keep the store they open today."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from kitlib import pmdata
from kitlib.backlog import opener

from tests.backlog.helpers import TODAY, priced
from tests.gitfixture import TempRepo, fixture_env
from tests.home import temp_home

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'backlog.py')
STORE = 'kit/backlog'
BRANCH = 'kit/wk'
DEFAULT_REFUSED = 'refs/backlog/main is the default backlog store'


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


class WorktreeLiveStore(unittest.TestCase):

    def setUp(self):
        patch = mock.patch.dict(os.environ, fixture_env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.home = temp_home(self)
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        pmdata.write(self.repo.common_dir(), 'pmdata', 'private', self.repo.head())
        self.pm = self.repo.worktree('pm', branch='pmdata')
        self.repo.write('.kit.json', json.dumps({'backlog': {'store': STORE}}) + '\n', root=self.pm)
        text = priced('FOO', state='pulled', pulled_at=TODAY, pulled_by=BRANCH, scope='tools/x/**')['FOO']
        self.repo.write(STORE + '/FOO.md', text, root=self.pm)
        self.repo.commit('the live store', '.kit.json', STORE + '/FOO.md', cwd=self.pm)
        self.wt = self.repo.worktree('wk', branch=BRANCH)

    def record_file(self):
        with open(os.path.join(self.pm, STORE, 'FOO.md'), encoding='utf-8') as f:
            return f.read()

    def run_tool(self, root, *args):
        env = fixture_env({'USERPROFILE': self.home, 'HOME': self.home})
        p = subprocess.run([sys.executable, TOOL, '-C', root] + list(args), capture_output=True,
                           text=True, encoding='utf-8', env=env, timeout=120)
        return p.returncode, p.stdout + p.stderr

    def test_a_worktree_opens_the_live_store_as_its_branch(self):
        ctx = opener.open_store(self.wt, fetch=False)
        self.assertTrue(same_path(ctx.root, self.pm), ctx.root)
        self.assertEqual(ctx.branch, BRANCH)
        self.assertEqual(ctx.current_branch(), BRANCH)
        rec = ctx.store.load().get('FOO')
        self.assertIsNotNone(rec, 'the worktree read the dead default ref, not the live store')
        self.assertEqual(rec.get('pulled_by'), BRANCH)

    def test_an_as_branch_is_kept(self):
        ctx = opener.open_store(self.wt, fetch=False, branch='kit/other')
        self.assertTrue(same_path(ctx.root, self.pm), ctx.root)
        self.assertEqual(ctx.branch, 'kit/other')

    def test_the_bare_worktree_run_reads_and_writes_the_live_store(self):
        rc, out = self.run_tool(self.wt, '--no-fetch', 'show', 'FOO')
        self.assertEqual(rc, 0, out)
        self.assertIn('pulled_by: %s' % BRANCH, out)
        rc, out = self.run_tool(self.wt, '--no-fetch', 'set', 'FOO', '--scope', 'tools/x/a.py')
        self.assertEqual(rc, 0, out)
        self.assertNotIn(DEFAULT_REFUSED, out)
        self.assertIn('scope: tools/x/a.py', self.record_file())
        self.assertNotIn('tools/x/**', self.record_file())

    def test_the_main_checkout_keeps_the_default_store(self):
        ctx = opener.open_store(self.repo.root, fetch=False)
        self.assertTrue(same_path(ctx.root, self.repo.root), ctx.root)
        self.assertIsNone(ctx.store.load().get('FOO'))
        rc, out = self.run_tool(self.repo.root, '--no-fetch', 'set', 'FOO', '--scope', 'tools/x/a.py')
        self.assertNotEqual(rc, 0, out)
        self.assertIn('tools/x/**', self.record_file())

    def test_the_pm_data_worktree_opens_its_own_store(self):
        ctx = opener.open_store(self.pm, fetch=False)
        self.assertTrue(same_path(ctx.root, self.pm), ctx.root)
        self.assertIsNone(ctx.branch)
        self.assertEqual(ctx.current_branch(), 'pmdata')

    def test_a_worktree_naming_its_own_store_keeps_it(self):
        self.repo.write('.kit.json', json.dumps({'backlog': {'store': 'ref'}}) + '\n', root=self.wt)
        ctx = opener.open_store(self.wt, fetch=False)
        self.assertTrue(same_path(ctx.root, self.wt), ctx.root)
        self.assertIsNone(ctx.store.load().get('FOO'))

    def test_a_detached_worktree_keeps_its_own_store(self):
        self.repo.git('checkout', '-q', '--detach', cwd=self.wt)
        ctx = opener.open_store(self.wt, fetch=False)
        self.assertTrue(same_path(ctx.root, self.wt), ctx.root)

    def test_no_live_store_keeps_its_own(self):
        self.repo.write('.kit.json', json.dumps({'backlog': {}}) + '\n', root=self.pm)
        ctx = opener.open_store(self.wt, fetch=False)
        self.assertTrue(same_path(ctx.root, self.wt), ctx.root)


if __name__ == '__main__':
    unittest.main()
