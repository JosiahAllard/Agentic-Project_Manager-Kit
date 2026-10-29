"""The gate tests' stack dump (tests.gate.fixtures.arm_dump): a GateRepoCase test that runs past
DUMP_S after its setUp ends writes every thread's stack, naming the test, to DUMP_FD; one that
ends in time writes nothing, then or later; an fd faulthandler cannot use skips the dump."""
import faulthandler
import os
import tempfile
import time
import unittest
from unittest import mock

from tests.gate import fixtures

DUMP_S = 0.5            # the patched deadline, counted from the end of the probe's setUp
SLOW_S = 1.5            # a probe this slow is dumped (at 0.5 s, and again at 1.0 s: repeat)
QUICK_DUMP_S = 1.0      # the quick probe's deadline: room for a loaded machine between its setUp
                        # and its cleanup, yet still inside AFTER_S
AFTER_S = 1.2           # past the deadlines: a dump not cancelled would have written by then


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

    def run_probe(self, cls, dump_s=DUMP_S):
        """Runs one probe with the deadline dump_s and the dump going to self.path; its result."""
        result = unittest.TestResult()
        with mock.patch.object(fixtures, 'DUMP_S', dump_s, create=True), \
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
        self.run_probe(probe(0), QUICK_DUMP_S)
        time.sleep(AFTER_S)
        self.assertEqual(self.dumped(), '')

    def test_the_deadline_fires_well_inside_the_pools_unit_timeout(self):
        from kitlib.tooldeps import selfrun
        self.assertLessEqual(fixtures.DUMP_S, selfrun.UNIT_TIMEOUT / 2)

    def test_an_unusable_fd_skips_the_dump(self):
        self.addCleanup(faulthandler.cancel_dump_traceback_later)     # should it arm after all
        case = mock.Mock()
        with mock.patch.object(fixtures, 'DUMP_FD', -1, create=True):
            self.assertFalse(fixtures.arm_dump(case))
        case.addCleanup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
