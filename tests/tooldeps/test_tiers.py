"""kitlib.tooldeps.tiers: kit_tests --both-tiers' pass loop and its verdict lines."""
import io
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


if __name__ == '__main__':
    unittest.main()
