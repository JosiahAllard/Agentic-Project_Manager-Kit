"""The process primitives on Linux (docs/linux-support-plan.md section 8, slice 4): /proc readers
behind kitlib.proc.process's names, the ctime `(boot << 64) | starttime`. On a real spawned tree:
ancestry, descendants, command_line and kill_tree; a parent created after its child ends a walk;
a pid at another ctime is not killed; an ended, unreaped process (a zombie) is gone.

Backends runs on every platform: the Linux backend's process area is that module, and on Windows
kitlib.proc.process keeps its own functions."""
import importlib
import os
import signal
import subprocess
import sys
import time
import unittest

from kitlib.platform import api
from kitlib.proc import process
from tests.platforms import posix_only, windows_only

# A child that starts a grandchild, prints the grandchild's pid and sleeps; both carry a marker
# argument so command_line can be checked.
CHILD = ('import subprocess, sys, time\n'
         'g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", '
         '"kit-grandchild-marker"])\n'
         'print(g.pid, flush=True)\n'
         'time.sleep(60)\n')
LOW64 = (1 << 64) - 1


def procfs():
    return importlib.import_module('kitlib.platform.linux.process')


def starttime(pid):
    """Field 22 of /proc/<pid>/stat, read here on its own."""
    with open('/proc/%d/stat' % pid, 'rb') as f:
        data = f.read()
    return int(data[data.rfind(b')') + 2:].split()[19])


def wait_until(test, what, timeout=10.0):
    end = time.monotonic() + timeout
    while not test():
        if time.monotonic() > end:
            raise AssertionError('not within %g s: %s' % (timeout, what))
        time.sleep(0.02)


class Backends(unittest.TestCase):
    def test_the_linux_process_area_is_the_proc_readers(self):
        linux = importlib.import_module('kitlib.platform.linux')
        self.assertIs(linux.BUILT['process'], procfs())
        for name in api.PROCESS:
            with self.subTest(name=name):
                self.assertIs(getattr(linux, name), getattr(procfs(), name))

    @windows_only('Windows keeps the Win32 process functions')
    def test_windows_keeps_its_own_process_functions(self):
        for name in ('snapshot', 'alive', 'proc_ctime', 'own_ctime', 'ancestry', 'descendants',
                     'command_line', 'terminate_verified', 'kill_tree', '_ancestor'):
            with self.subTest(name=name):
                self.assertEqual(getattr(process, name).__module__, 'kitlib.proc.process')

    @posix_only('kitlib.proc.process reads /proc on Linux')
    def test_kitlib_proc_process_names_are_the_proc_readers_on_linux(self):
        for name in ('snapshot', 'process_parents', 'listed_ctimes', 'alive', 'proc_ctime',
                     'own_ctime', '_ancestor', 'ancestry', 'own_ancestors', 'descendants',
                     'command_line', 'terminate_verified', 'kill_tree'):
            with self.subTest(name=name):
                self.assertIs(getattr(process, name), getattr(procfs(), name))


@posix_only('the process primitives read /proc on Linux')
class Tree(unittest.TestCase):
    def setUp(self):
        self.child = subprocess.Popen([sys.executable, '-c', CHILD, 'kit-child-marker'],
                                      stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._end)
        self.gpid = int(self.child.stdout.readline())
        self.ct = process.proc_ctime(self.child.pid)
        self.gct = process.proc_ctime(self.gpid)
        self.own = process.own_ctime()

    def _end(self):
        for pid, ct in ((self.gpid, getattr(self, 'gct', None)), (self.child.pid, self.ct)):
            if ct is not None:
                process.terminate_verified(pid, ct)     # this test's own, verified
        self.child.wait(30)
        self.child.stdout.close()

    def test_the_ctime_is_the_boot_and_the_starttime(self):
        self.assertEqual(self.ct >> 64, procfs().boot())
        self.assertEqual(self.ct & LOW64, starttime(self.child.pid))
        self.assertEqual(self.own & LOW64, starttime(os.getpid()))
        self.assertLessEqual(self.own, self.ct)
        self.assertLessEqual(self.ct, self.gct)

    def test_ancestry_walks_up_through_the_child_to_this_process(self):
        ancestors, exited = process.ancestry(self.gpid, self.gct)
        self.assertEqual(ancestors[:2], [(self.child.pid, self.ct), (os.getpid(), self.own)])
        self.assertFalse(exited)
        self.assertIn((os.getpid(), self.own), process.ancestors_of(self.child.pid, self.ct))

    def test_a_parent_created_after_its_child_ends_the_walk(self):
        # This process named as the child's child: its "parent" is newer, a reused pid.
        self.assertEqual(process.ancestry(os.getpid(), self.own,
                                          parents={os.getpid(): self.child.pid}), ([], False))
        self.assertEqual(process.ancestry(self.child.pid, self.ct,
                                          parents={self.child.pid: os.getpid()}),
                         ([(os.getpid(), self.own)], False))

    def test_descendants_lists_the_tree_parents_first(self):
        tree = process.descendants(os.getpid(), self.own)
        self.assertIn((self.child.pid, self.ct), tree)
        self.assertIn((self.gpid, self.gct), tree)
        self.assertLess(tree.index((self.child.pid, self.ct)), tree.index((self.gpid, self.gct)))
        self.assertEqual(process.descendants(self.child.pid, self.ct), [(self.gpid, self.gct)])

    def test_command_line_is_the_arguments_and_none_at_another_ctime(self):
        line = process.command_line(self.child.pid, self.ct)
        self.assertTrue(line.startswith(sys.executable + ' -c '), line)
        self.assertTrue(line.endswith(' kit-child-marker'), line)
        self.assertTrue(process.command_line(self.gpid).endswith(' kit-grandchild-marker'))
        self.assertIsNone(process.command_line(self.child.pid, self.ct + 1))

    def test_a_pid_at_another_ctime_is_not_killed(self):
        self.assertEqual(process.terminate_verified(self.child.pid, self.ct - 1), 'reused')
        self.assertTrue(process.alive(self.child.pid, self.ct))
        self.assertFalse(process.alive(self.child.pid, self.ct - 1))
        self.assertIsNone(self.child.poll())

    def test_kill_tree_ends_the_child_and_the_grandchild(self):
        self.assertEqual(process.kill_tree(self.child.pid, self.ct),
                         {self.child.pid: 'killed', self.gpid: 'killed'})
        self.assertEqual(self.child.wait(30), -signal.SIGKILL)
        wait_until(lambda: not process.alive(self.gpid, self.gct), 'the grandchild ended')
        self.assertFalse(process.alive(self.child.pid, self.ct))
        self.assertIn(process.terminate_verified(self.child.pid, self.ct), ('gone', 'reused'))


@posix_only('the process primitives read /proc on Linux')
class Ended(unittest.TestCase):
    def test_a_zombie_is_gone_and_names_its_parent(self):
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        self.addCleanup(p.wait, 30)
        ct = process.proc_ctime(p.pid)
        self.assertIsNotNone(ct)
        # Not waited for: once it exits it stays a zombie of this process until p.wait().
        wait_until(lambda: process._ancestor(p.pid)[1], 'the child is a zombie')
        self.assertEqual(process._ancestor(p.pid), (ct, True, os.getpid()))
        self.assertFalse(process.alive(p.pid, ct))
        self.assertFalse(process.alive(p.pid, None))
        self.assertEqual(process.terminate_verified(p.pid, ct), 'gone')

    def test_no_such_pid_is_gone(self):
        with open('/proc/sys/kernel/pid_max') as f:
            pid = int(f.read()) + 1
        self.assertIsNone(process.proc_ctime(pid))
        self.assertFalse(process.alive(pid, None))
        self.assertIsNone(procfs().identity(pid))
        self.assertFalse(process.alive(0, None))

    def test_a_ctime_that_is_not_a_number_cannot_be_told(self):
        self.assertTrue(process.alive(os.getpid(), 'not a number'))
        self.assertTrue(process.alive(os.getpid(), None))


if __name__ == '__main__':
    unittest.main()
