"""Process helpers for kit tests that check the pid shield (kitlib.proc.shield): a child's own
probe of its shield, and how long an OUTER shield keeps a new process of this one shielded.
Ported from the PM kit's _testutil.py (kit/pid-hold d151596): only these two; its rmtree_force is
tests/gitfixture.py's rmtree here, and its fixture runner, git spawns and template repos are not
needed by any kit test.
"""
import os
import subprocess
import sys
import time

WINDOWS = os.name == 'nt'

# A process's own check, from inside: may it be opened for PROCESS_TERMINATE? Source text that a
# test prepends to a child's `-c` code; `shielded()` is True only on ERROR_ACCESS_DENIED (5), the
# pid shield (kitlib.proc.shield) at work.
SHIELDED = '''
def shielded():
    import ctypes, os
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    h = k.OpenProcess(0x0001, False, os.getpid())
    if h:
        k.CloseHandle(ctypes.c_void_p(h))
        return False
    return ctypes.get_last_error() == 5
'''

_REACH = []         # [ancestor_reach()'s answer], once measured


def can_terminate(pid):
    """True if this process may open `pid` for PROCESS_TERMINATE (it only asks; it kills
    nothing)."""
    import ctypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = (ctypes.c_void_p,)
    h = k.OpenProcess(0x0001, False, pid)
    if h:
        k.CloseHandle(h)
        return True
    return False


def ancestor_reach():
    """Seconds an outer shield keeps a NEW process of this one shielded, measured once per
    process; 0.0 when nothing does (and off Windows).

    WHY: the kit shields only the processes it starts itself (kitlib.pytests' units), never a
    tree. But the older kit's gate (its pid_shield.TreeShield) shields every new process of a step
    for its grace whatever the code under test does, and these tests may run under one. A check
    that a process is UNshielded reads its DACL only after this many seconds. Measured on a
    canary child the test process starts directly: is it shielded within 1 s, and if so, for how
    long (plus 0.5 s)."""
    if _REACH:
        return _REACH[0]
    reach = 0.0
    if WINDOWS:
        canary = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                                  stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        start = time.monotonic()
        try:
            while time.monotonic() - start < 1.0 and can_terminate(canary.pid):
                time.sleep(0.005)
            if not can_terminate(canary.pid):
                while not can_terminate(canary.pid) and time.monotonic() - start < 120:
                    time.sleep(0.02)
                reach = time.monotonic() - start + 0.5
        finally:
            canary.stdin.close()            # it ends at EOF; never killed by its pid
            try:
                canary.wait(timeout=30)
            except subprocess.TimeoutExpired:
                canary.kill()               # our own handle
                canary.wait()
    _REACH.append(reach)
    return reach


def dead_process():
    """A process that has ended, as its Popen: a lock holder that is surely gone.

    Keep the Popen for as long as the test uses its pid. On Windows a pid is not given to a new
    process while any handle to the old one is open, and the Popen holds one from creation until
    it is garbage-collected: returning only `.pid` drops it, and a busy machine can then reuse the
    pid, so a "gone" holder reads as alive (KIT-DEADPID-REUSE). As a second check the pid is asked
    again (kitlib.records.landing.process_alive) and another process tried if it reads alive."""
    from kitlib.records import landing
    for _ in range(50):
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        p.wait()
        if not landing.process_alive(p.pid, None):
            return p
    raise AssertionError('no ended process kept a dead pid in 50 tries')


def exited_object(pid):
    """True when `pid` opens (SYNCHRONIZE) to a process that has exited: a held, unreused pid.
    Windows only."""
    import ctypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    k.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    k.CloseHandle.argtypes = (ctypes.c_void_p,)
    h = k.OpenProcess(0x00100000, False, pid)
    if not h:
        return False
    try:
        return k.WaitForSingleObject(h, 0) == 0                 # WAIT_OBJECT_0: it has exited
    finally:
        k.CloseHandle(h)
