"""The home fence (docs/test-tools.md "Home fence"): in a test process kitlib.config.user.home_dir()
refuses the real home folder; a temp home passes; a process that does not import the tests
package is not fenced."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

import tests
from kitlib.config import user
from tests import gitfixture
from tests.home import fence_decoy, temp_home

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class HomeFenceTest(unittest.TestCase):
    def test_the_real_home_is_refused_in_a_test_process(self):
        # resolving the home is a path computation only: nothing under it is read
        with self.assertRaises(user.HomeFenced) as cm:
            user.home_dir()
        self.assertIn('Home fence', str(cm.exception))

    def test_a_temp_home_passes(self):
        home = temp_home(self)
        self.assertEqual(os.path.normcase(user.home_dir()), os.path.normcase(home))
        self.assertIsNone(user.user_config()['ledger_path'])

    def test_a_fenced_home_refuses_every_lookup_through_it(self):
        fence_decoy(self)
        with self.assertRaises(user.HomeFenced):
            user.home_dir()
        with self.assertRaises(user.HomeFenced):
            user.user_config()
        with self.assertRaises(user.HomeFenced):
            user.locate()
        given = tempfile.mkdtemp(prefix='kit-given-home-')
        self.addCleanup(gitfixture.rmtree, given)
        self.assertEqual(user.locate(home=given).state, user.NONE)      # an explicit home passes

    def test_importing_the_tests_package_set_the_fence(self):
        self.assertIs(tests._kuser, user)
        self.assertIsNotNone(user._FENCED_HOME)

    def test_except_exception_does_not_swallow_the_refusal(self):
        fence_decoy(self)
        self.assertFalse(issubclass(user.HomeFenced, Exception))
        with self.assertRaises(user.HomeFenced):
            try:
                user.home_dir()
            except Exception:           # how kitlib isolates one check
                self.fail('HomeFenced was caught as an Exception')


class RealRunTest(unittest.TestCase):
    """A guard, not a red-first test (green before the fence too): a process that imports kitlib
    and not the tests package resolves the home as ever."""

    def test_a_process_without_the_tests_package_is_not_fenced(self):
        d = tempfile.mkdtemp(prefix='kit-fence-child-')
        self.addCleanup(gitfixture.rmtree, d)
        script = os.path.join(d, 'child.py')
        with open(script, 'w', encoding='utf-8') as fh:
            fh.write(textwrap.dedent('''\
                import os, sys
                sys.path.insert(0, sys.argv[1])
                from kitlib.config import user
                print(user.home_dir() == os.path.expanduser('~'), 'tests' in sys.modules,
                      getattr(user, '_FENCED_HOME', None) is None)
                '''))
        r = subprocess.run([sys.executable, '-I', script, ROOT], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(r.stdout.decode().split(), ['True', 'False', 'True'])


if __name__ == '__main__':
    unittest.main()
