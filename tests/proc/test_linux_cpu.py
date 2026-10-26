"""The affinity primitives on Linux (kitlib.platform.linux.cpu; docs/linux-support-plan.md
section 8, slice 5): a mask's cpu ids, and on a real child affinity_of and set_affinity with the
answers kitlib.proc.process gives on Windows: pinned, gone, reused, failed (an empty mask)."""
import importlib
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.platforms import posix_only


def lcpu():
    return importlib.import_module('kitlib.platform.linux.cpu')


class Masks(unittest.TestCase):
    def test_a_mask_is_its_cpu_ids_ascending(self):
        c = lcpu()
        for mask, ids in ((0, []), (None, []), (1, [0]), (0b1010, [1, 3]), (1 << 70, [70])):
            with self.subTest(mask=mask):
                self.assertEqual(c.mask_cpus(mask), ids)
                self.assertEqual(c.cpus_mask(ids), mask or 0)

    def test_an_empty_mask_is_refused_before_any_process_is_touched(self):
        c = lcpu()
        with mock.patch.object(c.process, '_pidfd_open', side_effect=AssertionError('opened')):
            self.assertEqual(c.set_affinity(1, 2, 0), 'failed')


@posix_only('sched_setaffinity on a real child')
class RealChild(unittest.TestCase):
    def setUp(self):
        self.c = lcpu()
        self.child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(self.child.wait)
        self.addCleanup(self.child.kill)
        self.ctime = self.c.process.proc_ctime(self.child.pid)
        self.cpus = sorted(os.sched_getaffinity(0))

    def test_a_child_is_pinned_and_read_back(self):
        one = self.cpus[-1]
        self.assertEqual(self.c.set_affinity(self.child.pid, self.ctime, 1 << one), 'pinned')
        self.assertEqual(os.sched_getaffinity(self.child.pid), {one})
        self.assertEqual(self.c.affinity_of(self.child.pid, self.ctime), 1 << one)

    def test_another_ctime_is_a_reused_pid_and_nothing_is_set(self):
        before = os.sched_getaffinity(self.child.pid)
        self.assertEqual(self.c.set_affinity(self.child.pid, self.ctime + 1, 1 << self.cpus[0]),
                         'reused')
        self.assertEqual(os.sched_getaffinity(self.child.pid), before)
        self.assertIsNone(self.c.affinity_of(self.child.pid, self.ctime + 1))

    def test_an_ended_child_is_gone(self):
        self.child.kill()
        self.child.wait()
        self.assertEqual(self.c.set_affinity(self.child.pid, self.ctime, 1 << self.cpus[0]),
                         'gone')
        self.assertIsNone(self.c.affinity_of(self.child.pid, self.ctime))

    def test_an_unreaped_child_is_gone(self):
        self.child.kill()
        os.waitid(os.P_PID, self.child.pid, os.WEXITED | os.WNOWAIT)    # a zombie
        self.assertEqual(self.c.set_affinity(self.child.pid, self.ctime, 1 << self.cpus[0]),
                         'gone')

    def test_a_mask_of_no_allowed_cpu_fails(self):
        self.assertEqual(self.c.set_affinity(self.child.pid, self.ctime, 1 << 1000), 'failed')


if __name__ == '__main__':
    unittest.main()
