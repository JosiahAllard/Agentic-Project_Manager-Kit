"""no_window.py: run a program with no console window. For a scheduled task (docs/daily-full.md,
The schedule; docs/worktree-remove.md, The scheduled task): the task runs this with pythonw.exe,
which has no console, so the program gets a hidden one and everything it starts shares it.

  pythonw no_window.py PROGRAM [ARGS...]

PROGRAM `python` (or `python.exe`) is the python.exe beside the pythonw.exe running this; any other
PROGRAM is started as named. It waits for PROGRAM and exits with its exit code, the same 32 bits
whatever they are (a crash's 0xC0000005 too: kitlib.proc.spawn.exit_code). Run from a console
(python.exe) it changes nothing: PROGRAM shares that console.

A thin entry point (ARCHITECTURE.md, Layers): kitlib.proc.spawn.carry does the work. It prints one
thing, USAGE on stderr when no PROGRAM is named, and only when it has a stderr (pythonw has none
unless whoever started it gave it one; a scheduled task gives none). Exit codes: PROGRAM's own;
2 no PROGRAM named; 127 PROGRAM could not be started.

So that a failure is not silent with no stderr, an exit of 127 and an exception out of the carry
each append one JSON line (PROGRAM's file name, never its arguments; for an exception, its type
only) to the kit's console incident log (kitlib.proc.console.note_carrier_failure, how carrier-fail); a line that
cannot be written is skipped and changes nothing else. A PROGRAM that itself exits 127 is logged too:
the exit code alone cannot tell the two apart.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from kitlib.proc import spawn                                       # noqa: E402

USAGE = 'USAGE: pythonw no_window.py PROGRAM [ARGS...]'


def note(argv, why):
    """The incident line; a failure to write it never replaces the exit code or the exception."""
    try:
        from kitlib.proc import console   # only on the way out: a carried run starts no sooner
        console.note_carrier_failure(argv[0], why)
    except Exception:
        pass


def main(argv):
    try:
        code = spawn.carry(argv)
    except Exception as e:
        note(argv, type(e).__name__)      # the type only: a message can echo the arguments
        raise
    if argv and code == spawn.EXIT_NO_START:
        note(argv, 'exit %d (it could not be started, or it exited %d itself)' % (code, code))
    if not argv and sys.stderr is not None:
        sys.stderr.write(USAGE + '\n')
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
