"""Process ancestry read one process at a time, never from a machine-wide listing.

kitlib's ancestors_of reads parents from a Toolhelp snapshot by default: a listing of
every process on the machine for each walk. Verifying one hook walked ancestry 13 times,
so on a busy machine those listings were most of the service's work and outlasted the
hook's request budget; the hook then exits with nothing published. The walk here asks
each process for its own recorded parent, so its cost follows the hops walked, not the
machine's process count.
"""
from kitlib.proc import process
from kitlib.proc.win32 import K32


class _RunningParents:
    """parents.get(pid) as a snapshot answers it: the recorded parent pid of a running
    process, else None."""

    def get(self,pid):
        handle=process.open_process(pid,process.PROCESS_QUERY_LIMITED_INFORMATION|process.SYNCHRONIZE)
        if not handle: return None
        try:
            # A snapshot lists running processes only. An exited one stays openable while
            # any handle to it is open, so the walk must not pass through it here either.
            if K32.WaitForSingleObject(handle,0)!=process.WAIT_TIMEOUT: return None
            return process.parent_pid(handle)
        finally: process.close(handle)


def ancestors_of(pid,creation, *, limit):
    """kitlib's ancestors_of with each parent read from that process alone."""
    return process.ancestors_of(pid,creation,parents=_RunningParents(),limit=limit)
