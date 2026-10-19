"""The bridge's ancestry walk reads each parent from that process, as a snapshot would list it."""
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from optional.bridge.win import ancestry
from kitlib.proc import process


class _Process:
    def __init__(self,creation,parent): self.creation,self.parent=creation,parent


class _ReusedPidKernel:
    """Pid 2000 is the true parent at its first open and a newer process, whose parent is
    an older unrelated one, at every later open: the pid was reused mid-walk."""
    def __init__(self):
        self.table={1000:[_Process(300,2000)],2000:[_Process(200,3000),_Process(400,4000)],
            3000:[_Process(100,0)],4000:[_Process(50,0)]}
        self.opens={}

    def open_process(self,pid,access=None):
        pid=int(pid)
        if pid not in self.table: return None
        count=self.opens[pid]=self.opens.get(pid,0)+1
        return self.table[pid][min(count,len(self.table[pid]))-1]

    def proc_ctime(self,pid):
        handle=self.open_process(pid)
        return handle.creation if handle else None

    def patches(self):
        running=type('K32',(),{'WaitForSingleObject':staticmethod(lambda handle,wait:process.WAIT_TIMEOUT)})
        return (patch('kitlib.proc.process.open_process',self.open_process),
            patch('kitlib.proc.process.proc_ctime',self.proc_ctime),
            patch('kitlib.proc.process.handle_ctime',lambda handle:handle.creation),
            patch('kitlib.proc.process.parent_pid',lambda handle:handle.parent),
            patch('kitlib.proc.process.close',lambda handle:None),
            patch.object(ancestry,'K32',running))


class _NewerParentKernel(_ReusedPidKernel):
    """Child 300 records parent 400, but the process now at pid 400 was created after the
    child: the true parent exited and its pid went to an unrelated process."""
    def __init__(self):
        self.table={300:[_Process(300,400)],400:[_Process(500,1)],1:[_Process(10,0)]}
        self.opens={}


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

    def test_an_exited_process_has_no_ancestry_while_a_handle_keeps_it_openable(self):
        child=self.child()
        creation=process.proc_ctime(child.pid)
        child.stdin.close();child.wait(timeout=5)
        # Popen still holds the process handle, so the exited process can still be opened.
        self.assertIsNotNone(process.proc_ctime(child.pid))
        self.assertIsNone(ancestry._read(child.pid))
        self.assertEqual(ancestry.ancestors_of(child.pid,creation,limit=8),[])

    def test_a_process_other_than_the_recorded_one_has_no_ancestry(self):
        child=self.child()
        self.assertEqual(ancestry.ancestors_of(child.pid,process.proc_ctime(child.pid)+1,limit=8),[])

    def test_a_pid_reused_mid_walk_never_lends_the_new_process_parent(self):
        kernel=_ReusedPidKernel()
        patches=kernel.patches()
        for item in patches: item.start()
        try: walked=ancestry.ancestors_of(1000,300,limit=8)
        finally:
            for item in reversed(patches): item.stop()
        self.assertNotIn((4000,50),walked)
        self.assertEqual(walked,[(2000,200),(3000,100)])
        self.assertTrue(all(count==1 for count in kernel.opens.values()),kernel.opens)

    def test_a_parent_created_after_its_child_ends_the_walk(self):
        kernel=_NewerParentKernel()
        patches=kernel.patches()
        for item in patches: item.start()
        try: walked=ancestry.ancestors_of(300,300,limit=8)
        finally:
            for item in reversed(patches): item.stop()
        self.assertEqual(walked,[])
        self.assertNotIn(1,kernel.opens)  # never climbed past the unrelated process


if __name__=='__main__':
    unittest.main()
