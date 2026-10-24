"""kitlib.proc.process ancestry on real processes: the recorded parent of an exited ancestor is
read from its own handle (NtQueryInformationProcess), so the walk past it reaches this process.
tests/proc/test_ancestry.py fakes that read; this test does not."""
import os
import subprocess
import sys
import unittest

from kitlib.proc import process

# middle starts a grandchild that sleeps, prints the grandchild's pid and exits. The test keeps
# middle's Popen handle, so middle has exited but can still be opened, as an exited shell can.
MIDDLE = ('import subprocess,sys;'
          'g=subprocess.Popen([sys.executable,"-I","-c","import time; time.sleep(120)"],'
          'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,'
          'creationflags=subprocess.CREATE_NO_WINDOW);print(g.pid,flush=True)')


class ExitedAncestor(unittest.TestCase):
    def test_the_walk_climbs_past_an_exited_held_ancestor_to_this_process(self):
        middle = subprocess.Popen([sys.executable, '-I', '-c', MIDDLE], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        grandchild, creation = None, None
        try:
            grandchild = int(middle.stdout.readline())
            creation = process.proc_ctime(grandchild)
            self.assertIsNotNone(creation)
            middle.wait(timeout=30)
            middle_ct = process.proc_ctime(middle.pid)
            self.assertIsNotNone(middle_ct)  # exited, and openable through the Popen handle
            # The real read this fix rests on: an exited handle still gives its recorded parent.
            self.assertEqual(process._ancestor(middle.pid), (middle_ct, True, os.getpid()))
            parents = process.process_parents()
            self.assertEqual(parents.get(grandchild), middle.pid)
            self.assertEqual(process.ancestry(grandchild, creation, parents, limit=8),
                             ([(middle.pid, middle_ct)], True))
            walked, _exited = process.ancestry(grandchild, creation, parents, limit=8,
                                               through_exited=True)
            self.assertEqual(walked[:2], [(middle.pid, middle_ct), (os.getpid(), process.own_ctime())])
        finally:
            middle.stdout.close()
            if grandchild is not None and creation is not None:
                # by pid and creation time only, never by name
                process.terminate_verified(grandchild, creation)
            middle.wait(timeout=30)


if __name__ == '__main__':
    unittest.main()
