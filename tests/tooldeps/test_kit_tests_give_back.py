"""tools/kit_tests.py and the lock's give back (docs/test-tools.md "kit_tests"): with no offer from
the lock run, one line says so, and the pool gets no give_back; with one, the pool gets the
drainer and no such line. The pool itself is mocked; the checkout is a throwaway repository."""
import importlib.util
import io
import os
import unittest
from unittest import mock

from tests import gitfixture

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('kit_tests_entry_for_give_back_tests',
                                               os.path.join(KIT, 'tools', 'kit_tests.py'))
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

GIVE_ENV = 'MACHINE_LOCK_GIVE_BACK'
NO_OFFER = 'kit_tests: give back: no offer'


class GiveBackOffer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = gitfixture.TempRepo()

    @classmethod
    def tearDownClass(cls):
        cls.repo.cleanup()

    def run_full(self, env):
        seen, out = {}, io.StringIO()

        def pool(modules, top, jobs, **kw):
            seen.update(kw)
            return 0, 'summary'
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(cli.selftree, 'config', return_value={'serial': []}), \
                mock.patch.object(cli.selftree, 'all_test_modules', return_value=['tests.x']), \
                mock.patch.object(cli.selfrun, 'run', side_effect=pool):
            rc = cli.main(['-C', self.repo.root, '--full', '--jobs', '2'], out=out)
        return rc, seen, out.getvalue()

    def test_no_offer_is_said_once(self):
        env = {k: v for k, v in os.environ.items() if k != GIVE_ENV}
        rc, seen, out = self.run_full(env)
        self.assertEqual(rc, 0, out)
        self.assertIsNone(seen['give_back'])
        self.assertEqual(out.count(NO_OFFER), 1, out)

    def test_an_offer_reaches_the_pool_and_says_nothing(self):
        rid = 'r-20261010T000000Z-abcdef'
        path = os.path.join(self.repo.root, 'store', 'active', rid + '.giveback')
        env = dict(os.environ, **{GIVE_ENV: path, 'MACHINE_LOCK_RUN': rid + ':1:2'})
        rc, seen, out = self.run_full(env)
        self.assertEqual(rc, 0, out)
        self.assertTrue(callable(seen['give_back']))
        self.assertNotIn(NO_OFFER, out)


if __name__ == '__main__':
    unittest.main()
