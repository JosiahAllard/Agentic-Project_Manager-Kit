"""kitlib.fs.replace: os.replace that waits out a reader holding the target open.

On Windows a rename over a file another process has open without delete sharing (an indexer, an
antivirus scan, a reader in another tool) fails with PermissionError for as long as that handle
stays open, usually a few milliseconds. replace() retries only that error, every PAUSE seconds for
at most WAIT seconds, then raises it. Any other OSError is raised at once. remove() is the same
for os.remove, and a file already gone is success.
"""
import os
import time

WAIT = 2.0      # seconds a refused replace is retried (kitlib.lock.store.write_json's bound)
PAUSE = 0.05    # seconds between its tries


def replace(src, dst, wait=None, pause=None):
    """os.replace(src, dst), retried on PermissionError for `wait` seconds (default WAIT)."""
    deadline = time.monotonic() + (WAIT if wait is None else wait)
    pause = PAUSE if pause is None else pause
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
        time.sleep(pause)


def remove(path, wait=None, pause=None):
    """os.remove(path), retried on PermissionError for `wait` seconds (default WAIT); a file
    that is already gone is not an error."""
    deadline = time.monotonic() + (WAIT if wait is None else wait)
    pause = PAUSE if pause is None else pause
    while True:
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
        time.sleep(pause)
