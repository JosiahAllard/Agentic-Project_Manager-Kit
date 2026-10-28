"""The wait the fake git gives a Windows sharing denial. The denial comes from another handle on
the file (another thread's rename, an antivirus or indexer scan, a pending delete) and clears when
that holder lets go, which a stalled machine can delay by seconds; so the wait is a time, never a
count of attempts, which a scheduling stall would burn through at once."""

import time

DEADLINE = 5.0      # seconds a denial is retried before it is raised
STEP = 0.01


def start():
    return time.monotonic()


def pause(began):
    """Sleep one step and return True while the deadline has not passed; False once it has."""
    if time.monotonic() - began >= DEADLINE:
        return False
    time.sleep(STEP)
    return True


def gave_up(exc, began):
    """`exc` (a PermissionError) rebuilt to name the time waited."""
    waited = time.monotonic() - began
    return PermissionError(exc.errno, '%s (still denied after waiting %.1f s)' % (exc.strerror, waited),
                           exc.filename)
