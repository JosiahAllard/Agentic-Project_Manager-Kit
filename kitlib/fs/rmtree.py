"""kitlib.fs.rmtree: shutil.rmtree that also removes read-only files.

git writes its object files read-only, and on Windows shutil.rmtree stops at the first one, so a
throwaway repository or clone would stay behind. rmtree_force clears the read-only bit of what
refuses to go and retries it once. kit_tests' run folders (kitlib.tooldeps.selfrun), the test
fixtures and the module map's isolation clones (kitlib.modulemap.isolate) remove theirs with it.
"""
import os
import shutil
import stat
import sys

_HAS_ONEXC = sys.version_info >= (3, 12)       # shutil.rmtree's onexc; before it, onerror


def rmtree_force(path, ignore_errors=False):
    """Remove the folder `path` the way shutil.rmtree does, but clear the read-only bit of what
    refuses to go and retry it once: git writes its objects read-only, and on Windows rmtree stops
    at the first one, so a throwaway repository would stay in TEMP. Python 3.12+ takes the handler
    as onexc, older as onerror. `ignore_errors` leaves what still stays; otherwise it raises.
    Only `path` and what is under it is touched; a missing `path` is not an error."""
    def clear(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            if not ignore_errors:
                raise
    if not os.path.lexists(path):
        return
    if _HAS_ONEXC:
        shutil.rmtree(path, onexc=clear)
    else:
        shutil.rmtree(path, onerror=clear)
