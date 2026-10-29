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
_REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


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


def plain_dir(path):
    """os.lstat of `path` when it is a folder itself; None for a link, junction or other reparse
    point, a file, or a path that cannot be read. A sweep removes only such a folder, so it never
    follows a link out of the folder it cleans."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or getattr(st, 'st_file_attributes', 0) & _REPARSE:
        return None
    return st
