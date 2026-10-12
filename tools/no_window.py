"""no_window.py: run a program with no console window. For a scheduled task (docs/daily-full.md,
The schedule; docs/worktree-remove.md, The scheduled task): the task runs this with pythonw.exe,
which has no console, so the program gets a hidden one and everything it starts shares it.

  pythonw no_window.py PROGRAM [ARGS...]

PROGRAM `python` (or `python.exe`) is the python.exe beside the pythonw.exe running this; any other
PROGRAM is started as named. It waits for PROGRAM and exits with its exit code. Run from a console
(python.exe) it changes nothing: PROGRAM shares that console.

A thin entry point (ARCHITECTURE.md, Layers): kitlib.proc.spawn.carry does the work. It never
prints (pythonw has no output to print to). Exit codes: PROGRAM's own; 2 no PROGRAM named;
127 PROGRAM could not be started.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from kitlib.proc import spawn                                       # noqa: E402

if __name__ == '__main__':
    sys.exit(spawn.carry(sys.argv[1:]))
