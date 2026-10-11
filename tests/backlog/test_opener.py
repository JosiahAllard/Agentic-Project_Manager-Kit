"""kitlib.backlog.opener in process (docs/backlog.md): open_store's defaults against a ref store on
a temporary repository with a bare remote, and open_repo's Failure outside a repository."""
import os
import tempfile
import unittest
from unittest import mock

from kitlib.backlog import errors, opener
from kitlib.backlog import record as rmod
from kitlib.backlog import store as smod
from tests.backlog.helpers import record_text
from tests.gitfixture import TempRepo, fixture_env, rmtree


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


class OpenStore(unittest.TestCase):
    """FOO is on the remote's store and the local cache of it is gone: only a fetch finds it."""

    def setUp(self):
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        home = self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(rmtree, home)
        # An empty home: no machine config, so no ledger path and no standing delegation are read
        # from the machine running the tests. The library's git inherits os.environ.
        env = mock.patch.dict(os.environ, fixture_env({'USERPROFILE': home, 'HOME': home}),
                              clear=True)
        env.start()
        self.addCleanup(env.stop)
        backend = smod.RefBackend(self.repo.root, 'origin', smod.STORE_REF, smod.STORE_REF)
        snap = smod.Store(backend, rmod.Rules()).load()
        backend.publish(snap, {'FOO': record_text()}, 'seed\n')
        self.repo.git('update-ref', '-d', smod.STORE_REF)

    def test_the_defaults_fetch_with_no_session_and_no_branch(self):
        ctx = opener.open_store(self.repo.root)
        self.assertIsNotNone(ctx.store.load().get('FOO'), 'the default open did not fetch')
        self.assertIs(ctx.store.backend.fetch, True)
        self.assertIsNone(ctx.session)
        self.assertIsNone(ctx.store.session)
        self.assertIsNone(ctx.branch)
        self.assertTrue(same_path(ctx.root, self.repo.root), ctx.root)
        self.assertEqual((ctx.remote, ctx.main_branch), ('origin', 'main'))
        self.assertIsNone(ctx.ledger.path)
        self.assertEqual(ctx.current_branch(), 'main')

    def test_each_argument_reaches_the_context(self):
        ctx = opener.open_store(self.repo.root, fetch=False, session='S-1', branch='tools/wk')
        self.assertIsNone(ctx.store.load().get('FOO'), 'fetch=False fetched')
        self.assertIs(ctx.store.backend.fetch, False)
        self.assertEqual(ctx.session, 'S-1')
        self.assertEqual(ctx.branch, 'tools/wk')

    def test_a_folder_that_is_no_repository_is_a_failure(self):
        with self.assertRaises(errors.Failure):
            opener.open_repo(os.path.join(self.home, 'no-such-dir'))


if __name__ == '__main__':
    unittest.main()
