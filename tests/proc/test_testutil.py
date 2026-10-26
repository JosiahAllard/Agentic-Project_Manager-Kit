"""Tests for tests/testutil.py: the SHIELDED probe tells a shielded child from an unshielded one,
ancestor_reach is measured once per process, and dead_process keeps its ended pid unused."""
import gc
import os
import subprocess
import sys
import unittest

from tests import testutil
from tests.platforms import windows_only

if os.name == 'nt':
    from kitlib.proc import shield, win32


@windows_only('the shield is Windows-only')
class TheProbe(unittest.TestCase):

    def run_probe(self, shielded):
        p = subprocess.Popen([sys.executable, '-c', testutil.SHIELDED + 'print(shielded())'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             creationflags=win32.CREATE_SUSPENDED)
        try:
            if shielded:
                shield.release(shield.shield(p))
            self.assertEqual(win32.resume(p), 0)
            out, err = p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()            # our own handle
                p.wait()
        return out.strip(), err

    def test_a_shielded_child_says_true(self):
        out, err = self.run_probe(True)
        self.assertEqual(out, 'True', err)

    def test_an_unshielded_child_says_false(self):
        if testutil.ancestor_reach():
            self.skipTest("an older kit's gate shields every new process of this one")
        out, err = self.run_probe(False)
        self.assertEqual(out, 'False', err)


@windows_only('pid reuse is checked on Windows')
class TheDeadProcess(unittest.TestCase):

    def test_its_pid_stays_an_exited_process_while_the_popen_is_kept(self):
        # A helper that kept only `.pid` dropped the Popen's handle, and the
        # pid was free for a new process. While the Popen is kept the pid opens to the exited
        # process object, which no new process can share.
        p = testutil.dead_process()
        self.assertIsNotNone(p.returncode)
        gc.collect()
        self.assertTrue(testutil.exited_object(p.pid))


class TheReach(unittest.TestCase):

    def test_it_is_measured_once(self):
        first = testutil.ancestor_reach()
        self.assertGreaterEqual(first, 0.0)
        self.assertEqual(testutil._REACH, [first])
        self.assertEqual(testutil.ancestor_reach(), first)


if __name__ == '__main__':
    unittest.main()
