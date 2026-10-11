"""kit_tests --list and kitlib.tooldeps.selflist (docs/test-tools.md "kit_tests"): the test ids,
the decorator skips and the import failures of throwaway test modules, loaded by a real child
process; and the flag's arguments and output through kit_tests.main."""
import importlib.util
import io
import os
import tempfile
import unittest
from unittest import mock

from kitlib.tooldeps import selflist
from tests import gitfixture

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('kit_tests_entry_for_list_tests',
                                               os.path.join(KIT, 'tools', 'kit_tests.py'))
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

ALPHA = '''import unittest


class Beta(unittest.TestCase):
    def test_two(self):
        pass

    def test_one(self):
        pass

    @unittest.skip('method reason')
    def test_skipped(self):
        pass

    def test_runtime(self):
        self.skipTest('runtime reason')


@unittest.skipIf(True, 'class reason')
class Gamma(unittest.TestCase):
    def test_g(self):
        pass
'''
OMEGA = '''import unittest


class Delta(unittest.TestCase):
    def test_d(self):
        pass
'''
BROKEN = 'import no_such_module_for_the_list_test\n'


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-list-')
        self.addCleanup(gitfixture.rmtree, self.root)
        pkg = os.path.join(self.root, 'tests', 'sample')
        os.makedirs(pkg)
        for d in (os.path.join(self.root, 'tests'), pkg):
            open(os.path.join(d, '__init__.py'), 'w').close()
        for name, text in (('test_alpha', ALPHA), ('test_omega', OMEGA)):
            with open(os.path.join(pkg, name + '.py'), 'w') as fh:
                fh.write(text)
        self.modules = ['tests.sample.test_alpha', 'tests.sample.test_omega']


class Collect(Fixture):
    def test_ids_are_complete_and_sorted(self):
        got = selflist.collect(self.root, self.modules)
        self.assertEqual([i for i, _b, _s in got], [
            'tests.sample.test_alpha.Beta.test_one', 'tests.sample.test_alpha.Beta.test_runtime',
            'tests.sample.test_alpha.Beta.test_skipped', 'tests.sample.test_alpha.Beta.test_two',
            'tests.sample.test_alpha.Gamma.test_g', 'tests.sample.test_omega.Delta.test_d'])

    def test_decorator_skips_carry_their_reason_and_runtime_skips_do_not(self):
        got = {i.rpartition('.')[2]: s for i, _b, s in selflist.collect(self.root, self.modules)}
        self.assertEqual(got['test_skipped'], 'method reason')
        self.assertEqual(got['test_g'], 'class reason')
        self.assertIsNone(got['test_runtime'])
        self.assertIsNone(got['test_one'])

    def test_a_module_that_fails_to_import_is_listed_and_flagged(self):
        with open(os.path.join(self.root, 'tests', 'sample', 'test_zbroken.py'), 'w') as fh:
            fh.write(BROKEN)
        got = selflist.collect(self.root, self.modules + ['tests.sample.test_zbroken'])
        broken = [i for i, b, _s in got if b]
        self.assertEqual(len(broken), 1, got)
        self.assertIn('test_zbroken', broken[0])
        lines = selflist.render(got, self.modules + ['tests.sample.test_zbroken'])
        self.assertIn('kit_tests: import failed: %s' % broken[0], lines)
        self.assertIn('1 module(s) failed to import', lines[7])

    def test_two_runs_render_identically(self):
        a = selflist.render(selflist.collect(self.root, self.modules), self.modules)
        b = selflist.render(selflist.collect(self.root, self.modules), self.modules)
        self.assertEqual(a, b)

    def test_summary_and_skip_lines(self):
        lines = selflist.render(selflist.collect(self.root, self.modules), self.modules)
        self.assertEqual(lines[6], 'kit_tests: list: 6 test(s) in 2 module(s), 2 static skip(s) '
                         '(runtime skips not counted), 0 module(s) failed to import')
        self.assertEqual(lines[7:], [
            'kit_tests: skip: tests.sample.test_alpha.Beta.test_skipped (method reason)',
            'kit_tests: skip: tests.sample.test_alpha.Gamma.test_g (class reason)'])


class Flag(Fixture):
    def run_main(self, *args):
        out = io.StringIO()
        rc = cli.main(['-C', self.root, '--list'] + list(args), out=out)
        return rc, out.getvalue()

    def test_list_prints_the_ids_and_runs_nothing(self):
        with mock.patch.object(cli.gitrepo, 'toplevel', return_value=(self.root, None)):
            rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        self.assertIn('tests.sample.test_omega.Delta.test_d\n', out)
        self.assertIn('kit_tests: list: 6 test(s) in 2 module(s), 2 static skip(s) (runtime '
                      'skips not counted)', out)

    def test_list_refuses_run_flags(self):
        for extra in (['--jobs', '1'], ['--plan'], ['--real-git'], ['--both-tiers'],
                      ['--pid-hold'], ['--changed', 'x'], ['--base', 'main']):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as cm:
                with mock.patch('sys.stderr', io.StringIO()):
                    self.run_main(*extra)
            self.assertEqual(cm.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
