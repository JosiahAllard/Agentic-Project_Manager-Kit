"""The bridge's ancestry walk reads each parent from that process, as a snapshot would list it."""
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from optional.bridge.win import ancestry
from kitlib.proc import process


class AncestryTests(unittest.TestCase):
    def child(self):
        child=subprocess.Popen([sys.executable,'-I','-c','import sys; sys.stdin.read()'],stdin=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(lambda:(child.stdin.closed or child.stdin.close(),child.wait(timeout=5)))
        return child

    def test_walk_matches_the_snapshot_walk_without_listing_the_machine(self):
        child=self.child()
        creation=process.proc_ctime(child.pid)
        expected=process.ancestors_of(child.pid,creation,limit=8)
        with patch('kitlib.proc.process.snapshot',wraps=process.snapshot) as listings:
            walked=ancestry.ancestors_of(child.pid,creation,limit=8)
        self.assertEqual(listings.call_count,0)
        self.assertEqual(walked[0],(os.getpid(),process.own_ctime()))
        self.assertEqual(walked,expected)

    def test_an_exited_process_has_no_parent_while_a_handle_keeps_it_openable(self):
        child=self.child()
        child.stdin.close();child.wait(timeout=5)
        # Popen still holds the process handle, so the exited process can still be opened.
        self.assertIsNotNone(process.proc_ctime(child.pid))
        self.assertIsNone(ancestry._RunningParents().get(child.pid))
        self.assertEqual(ancestry.ancestors_of(child.pid,None,limit=8),[])


if __name__=='__main__':
    unittest.main()
