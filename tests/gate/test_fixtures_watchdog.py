"""The gate tests' stack dump (tests.gate.fixtures.arm_dump): a GateRepoCase test that runs past
DUMP_S writes every thread's stack, naming the test, to DUMP_FD; one that ends in time writes
nothing, then or later."""
import os
import tempfile
import time
import unittest
from unittest import mock

from tests.gate import fixtures

DUMP_S = 0.5            # the patched deadline
SLOW_S = 1.5            # a probe this slow is dumped (at 0.5 s, and again at 1.0 s: repeat)
AFTER_S = 1.2           # past two deadlines: a dump not cancelled would have written by then


def probe(sleep_s):
    """A GateRepoCase whose test sleeps sleep_s; made here, so the loader never collects it."""
    class Probe(fixtures.GateRepoCase):
        def runTest(self):
            time.sleep(sleep_s)
    return Probe


class StackDump(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix='kit-dump-', suffix='.txt')
        self.fd = fd
        self.addCleanup(os.remove, self.path)
        self.addCleanup(os.close, fd)

    def run_probe(self, cls):
        """Runs one probe with the deadline DUMP_S and the dump going to self.path; its result."""
        result = unittest.TestResult()
        with mock.patch.object(fixtures, 'DUMP_S', DUMP_S, create=True), \
                mock.patch.object(fixtures, 'DUMP_FD', self.fd, create=True):
            cls('runTest').run(result)
        self.assertEqual((result.errors, result.failures), ([], []))
        return result

    def dumped(self):
        with open(self.path, encoding='utf-8', errors='replace') as f:
            return f.read()

    def test_a_slow_test_dumps_its_stack(self):
        self.run_probe(probe(SLOW_S))
        text = self.dumped()
        self.assertIn('most recent call first', text)
        self.assertIn('in runTest', text, text)
        self.assertIn('test_fixtures_watchdog.py', text, text)

    def test_the_cleanup_cancels_it(self):
        self.run_probe(probe(SLOW_S))
        size = len(self.dumped())
        self.assertGreater(size, 0, 'the slow probe was not dumped')
        time.sleep(AFTER_S)
        self.assertEqual(len(self.dumped()), size, 'a dump came after the test ended')

    def test_a_quick_test_dumps_nothing(self):
        self.run_probe(probe(0))
        time.sleep(AFTER_S)
        self.assertEqual(self.dumped(), '')


if __name__ == '__main__':
    unittest.main()
