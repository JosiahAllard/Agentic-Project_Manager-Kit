"""Process ancestry read one process at a time, never from a machine-wide listing.

kitlib's ancestors_of reads parents from a Toolhelp snapshot by default: a listing of
every process on the machine for each walk. Verifying one hook walked ancestry 13 times,
so on a busy machine those listings were most of the service's work and outlasted the
hook's request budget; the hook then exits with nothing published. The walk here asks
each process for its own recorded parent, so its cost follows the hops walked, not the
machine's process count.

Each pid is opened once, and its creation time, running state and recorded parent are
all read from that one handle. Reopening a pid to ask a second question could reach a
newer process that reused it, whose parent is not in this lineage; one handle cannot.
"""
from kitlib.proc import process
from kitlib.proc.win32 import K32


def _read(pid):
    """(creation, parent pid) of the running process pid, from one handle; else None.

    A snapshot lists running processes only. An exited one stays openable while any
    handle to it is open, so it ends the walk here too.
    """
    handle=process.open_process(pid,process.PROCESS_QUERY_LIMITED_INFORMATION|process.SYNCHRONIZE)
    if not handle: return None
    try:
        creation=process.handle_ctime(handle)
        if creation is None or K32.WaitForSingleObject(handle,0)!=process.WAIT_TIMEOUT: return None
        return creation,process.parent_pid(handle)
    finally: process.close(handle)


def ancestors_of(pid,creation, *, limit):
    """[(pid, creation)] of a process's running ancestors, nearest first, as kitlib's
    ancestors_of returns them, except that kitlib's walk takes an ancestor that has exited but
    is still held open as its last hop, where this one stops before it. The walk stops at a
    process that is gone or not running, is not the one recorded (creation differs), or was
    created after its child (its pid was reused)."""
    found=_read(pid)
    if found is None or (creation is not None and found[0]!=creation): return []
    out,current,current_creation,parent=[],int(pid),found[0],found[1]
    for _ in range(limit):
        if not parent or parent==current: break
        found=_read(parent)
        if found is None or found[0]>current_creation: break
        out.append((parent,found[0]))
        current,current_creation,parent=parent,found[0],found[1]
    return out
