"""A fake process table for ancestry walks (kitlib.proc.process ancestors_of, ancestry, and
kitlib.lock.store.caller): the Windows calls a walk makes are patched to answer from it, so a
chain no live machine can be made to show on demand (a shell that has exited but is still held
open, a reused parent pid) is one table.

Each process is FakeProc(ctime, parent, exited, listed, opens=True): `parent` is the parent pid
its handle records; `listed` is the exe name the listing gives it, or None when the listing no
longer names it; `opens` False is a process this user cannot open (another user's or the
system's). The listing's parent of a listed process is its recorded one, and the system's
process listing (process.system_processes) gives it its ctime. A pid not in the table cannot be
opened."""
import collections
import contextlib
from unittest import mock

from kitlib.proc import process, win32

FakeProc = collections.namedtuple('FakeProc', 'ctime parent exited listed opens', defaults=(True,))


class _K32:
    def __init__(self, table):
        self.table = table

    def WaitForSingleObject(self, h, _ms):
        return win32.WAIT_OBJECT_0 if self.table[h].exited else process.WAIT_TIMEOUT

    def CloseHandle(self, _h):
        return 1


def listing(table):
    """[(pid, ppid, exe)] as a snapshot of the table lists it."""
    return [(pid, p.parent, p.listed) for pid, p in table.items() if p.listed]


@contextlib.contextmanager
def patched(table, me):
    """The process calls answer from table while it is entered, and this process (pid `me`) has
    creation time table[me].ctime."""
    def opens(pid):
        return int(pid) in table and table[int(pid)].opens

    fakes = {
        'snapshot': lambda: listing(table),
        'system_processes': lambda: [(pid, p.ctime, p.listed)
                                     for pid, p in table.items() if p.listed],
        'own_ctime': lambda: table[me].ctime,
        'open_process': lambda pid, access=None: int(pid) if opens(pid) else None,
        'handle_ctime': lambda h: table[h].ctime,
        'parent_pid': lambda h: table[h].parent,
        'proc_ctime': lambda pid: table[int(pid)].ctime if opens(pid) else None,
        'K32': _K32(table),
    }
    with contextlib.ExitStack() as stack:
        for name, fake in fakes.items():
            stack.enter_context(mock.patch.object(process, name, fake))
        yield
