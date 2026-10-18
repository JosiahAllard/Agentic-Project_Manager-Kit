"""kitlib.tooldeps.tiers: kit_tests' two-tier pass loop and its verdict lines, and the modules the
real-git pass runs (the added or changed test modules, and --both-tiers' REAL_GIT_MODULES)."""
import io
import threading
import time
import unittest

from kitlib.tooldeps import tiers


class TierOutTest(unittest.TestCase):
    def test_kit_tests_lines_are_tagged_with_the_tier_and_others_pass_unchanged(self):
        out = io.StringIO()
        s = tiers.TierOut(out, 'real git')
        s.write('kit_tests: OK in ')          # a partial line waits for its end
        self.assertEqual(out.getvalue(), '')
        s.write('3s\nRan 4 tests\nkit_tests: jobs 2\nkit_tests: tail')
        s.flush()
        self.assertEqual(out.getvalue(), 'kit_tests [real git]: OK in 3s\nRan 4 tests\n'
                                         'kit_tests [real git]: jobs 2\nkit_tests [real git]: tail')


class YieldingOut(object):
    """A stream that gives up the thread on every write, as a console write can."""

    def __init__(self):
        self.parts = []

    def write(self, text):
        time.sleep(0)
        self.parts.append(text)

    def flush(self):
        pass


class TierOutThreadsTest(unittest.TestCase):
    def test_lines_written_from_many_threads_each_come_out_once_and_whole(self):
        out = YieldingOut()
        s = tiers.TierOut(out, 'real git')
        threads, per = 8, 200

        def hammer(t):
            for j in range(per):
                s.write('kit_tests: t%d line %d\n' % (t, j))
        workers = [threading.Thread(target=hammer, args=(t,)) for t in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        s.flush()
        got = sorted(''.join(out.parts).splitlines())
        want = sorted('kit_tests [real git]: t%d line %d' % (t, j)
                      for t in range(threads) for j in range(per))
        self.assertEqual(got, want)


class RunTiersTest(unittest.TestCase):
    def run_with(self, rcs):
        calls, out = [], io.StringIO()

        def one(fake, stream, last):
            calls.append((fake, last))
            stream.write('kit_tests: OK in 1s\n')
            return rcs[len(calls) - 1]
        return tiers.run_tiers(one, out), calls, out.getvalue()

    def test_both_green(self):
        rc, calls, text = self.run_with([0, 0])
        self.assertEqual((rc, calls), (0, [('1', False), ('0', True)]))
        self.assertTrue(text.endswith('kit_tests: OK, both tiers green (fake git, real git)\n'), text)
        self.assertIn('kit_tests: tier fake git: OK\n', text)
        self.assertIn('kit_tests: tier real git: OK\n', text)

    def test_one_red_tier_is_a_red_run(self):
        for rcs, red in (([1, 0], 'fake git'), ([0, 1], 'real git')):
            rc, calls, text = self.run_with(rcs)
            self.assertEqual((rc, len(calls)), (1, 2), text)
            self.assertNotRegex(text, r'(?m)^kit_tests: OK')
            self.assertTrue(text.endswith('red tier(s): %s\n' % red), text)

    def test_an_interrupted_pass_stops_the_run(self):
        rc, calls, text = self.run_with([tiers.INTERRUPTED, 0])
        self.assertEqual((rc, len(calls)), (tiers.INTERRUPTED, 1), text)
        self.assertNotRegex(text, r'(?m)^kit_tests: OK')

    def test_the_header_names_what_runs_the_tiers(self):
        out = io.StringIO()
        tiers.run_tiers(lambda fake, stream, last: 0, out, label='changed test modules')
        self.assertIn('kit_tests: changed test modules: tier fake git (KIT_FAKE_GIT=1)\n',
                      out.getvalue())
        rc, calls, text = self.run_with([0, 0])
        self.assertIn('kit_tests: --both-tiers: tier real git (KIT_FAKE_GIT=0)\n', text)


MODULES = ['tests.gate.test_land_x', 'tests.gate.test_other', 'tests.git.test_g',
           'tests.test_top']


class ChangedTestModules(unittest.TestCase):
    """The test modules a change adds or changes: the second pass on real git runs these."""

    def test_an_added_or_changed_test_module_is_named_in_the_modules_order(self):
        got = tiers.changed_test_modules(
            ['tests/git/test_g.py', 'tests\\gate\\test_other.py', 'tests/test_top.py'], MODULES)
        self.assertEqual(got, ['tests.gate.test_other', 'tests.git.test_g', 'tests.test_top'])

    def test_what_is_not_a_test_module_of_the_tree_is_left_out(self):
        changed = ['tests/gate/test_gone.py',        # deleted: the tree holds no such module
                   'tests/gate/fixtures.py',         # a helper
                   'tests/selection.json', 'kitlib/gate/land.py', 'docs/gate.md',
                   'tests/gate/test_other.txt', 'tools/test_like.py']
        self.assertEqual(tiers.changed_test_modules(changed, MODULES), [])
        self.assertEqual(tiers.changed_test_modules([], MODULES), [])

    def test_the_real_git_modules_take_the_changed_ones_too(self):
        self.assertEqual(tiers.real_git_modules(MODULES), ['tests.gate.test_land_x',
                                                           'tests.git.test_g'])
        self.assertEqual(tiers.real_git_modules(MODULES, also=['tests.test_top', 'tests.git.test_g']),
                         ['tests.gate.test_land_x', 'tests.git.test_g', 'tests.test_top'])
        # a changed module the list does not hold still runs
        self.assertEqual(tiers.real_git_modules(['tests.a.test_a'], also=['tests.b.test_b']),
                         ['tests.b.test_b'])


if __name__ == '__main__':
    unittest.main()
