"""kitlib.tooldeps.tiers: kit_tests' two-tier pass loop and its verdict lines, the modules the
real-git pass runs (the added or changed test modules, and --both-tiers' REAL_GIT_MODULES), and
what the fake pass keeps for it when it gives cpu slots back."""
import io
import os
import threading
import time
import unittest

from kitlib.tooldeps import selftree, tiers


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


KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ChangedTestModulesOfTheKitTree(unittest.TestCase):
    """changed_test_modules against this checkout's own module list (selftree.all_test_modules),
    the list kit_tests passes it, not a hand-made one: the dotted names it compares the paths to
    are the ones the tree walk really produces."""

    @classmethod
    def setUpClass(cls):
        cls.modules = selftree.all_test_modules(KIT)

    def test_this_module_and_a_top_level_one_are_found_by_either_slash(self):
        self.assertIn('tests.tooldeps.test_tiers', self.modules)
        got = tiers.changed_test_modules(['tests/tooldeps/test_tiers.py',
                                          'tests\\tooldeps\\test_selfrun.py'], self.modules)
        self.assertEqual(got, ['tests.tooldeps.test_selfrun', 'tests.tooldeps.test_tiers'])

    def test_every_module_of_the_tree_maps_back_to_itself(self):
        paths = [m.replace('.', '/') + '.py' for m in reversed(self.modules)]
        self.assertGreater(len(paths), 100)
        self.assertEqual(tiers.changed_test_modules(paths, self.modules), self.modules)

    def test_helpers_packages_and_data_of_the_tree_give_none(self):
        changed = [p for p in ('tests/__init__.py', 'tests/tooldeps/__init__.py',
                               'tests/gitfixture.py', 'tests/home.py', 'tests/selection.json',
                               'tools/kit_tests.py', 'kitlib/tooldeps/tiers.py')
                   if os.path.exists(os.path.join(KIT, *p.split('/')))]
        self.assertGreaterEqual(len(changed), 6)       # the fixture names files that exist
        self.assertEqual(tiers.changed_test_modules(changed, self.modules), [])


class UnitBoundAndKeepForNext(unittest.TestCase):
    """What the fake pass keeps for the real-git pass when it gives cpu slots back."""

    def test_a_module_counts_one_unit_per_class_when_it_has_two_or_more(self):
        classes = {'tests.a.test_split': ['A', 'B', 'C'], 'tests.a.test_one': ['A'],
                   'tests.a.test_none': []}
        self.assertEqual(tiers.unit_bound(['tests.a.test_split', 'tests.a.test_one',
                                           'tests.a.test_none', 'tests.a.test_uncounted'],
                                          classes), 6)
        self.assertEqual(tiers.unit_bound([], classes), 0)

    def test_the_earlier_pass_never_gives_back_below_the_next_pass_threads(self):
        asked = []
        give = tiers.keep_for_next(lambda n: asked.append(n) or 'ack', 3)
        self.assertEqual([give(n) for n in (7, 3, 2, 1)], ['ack'] * 4)
        self.assertEqual(asked, [7, 3, 3, 3])
        nothing_next = tiers.keep_for_next(asked.append, 0)
        nothing_next(1)
        self.assertEqual(asked[-1], 1)


if __name__ == '__main__':
    unittest.main()
