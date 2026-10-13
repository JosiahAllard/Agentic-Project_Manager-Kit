"""The guard's one path normaliser (multi-host plan section 6, rule 2).

Every place the guard folds a filesystem path to compare it, key a cache on it or build a canonical
form calls norm(). The rules are today's Windows ones, unchanged: os.path.normpath folds `.`,
`..`, doubled and mixed separators, os.path.normcase folds case (and `/` to `\\`) on Windows. The
later OS refactor changes them here, in one place.

  norm(path)                  the comparison form: normpath, then normcase
  norm(path, absolute=True)   the same of os.path.abspath(path) (relative to this process's folder)
  norm(path, real=True)       the same of os.path.realpath(path): links and junctions resolved; a
                              path realpath cannot read falls back to abspath
  norm(path, fold=False)      any of the above without the case fold: a path to show or join to

Not paths here: the text of a command line (drive letters, quoting, PowerShell provider paths) is
read by kitlib.guard.shell and commands, and a URL by kitlib.guard.prepush; those are a shell's
grammar, not the filesystem's.
"""
import os


def norm(path, absolute=False, real=False, fold=True):
    if real:
        try:
            p = os.path.realpath(path)
        except (OSError, ValueError):
            p = os.path.abspath(path)
    elif absolute:
        p = os.path.abspath(path)
    else:
        p = os.path.normpath(path)
    return os.path.normcase(p) if fold else p
