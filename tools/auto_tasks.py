"""auto_tasks.py: run the automatic tasks the Action Hub schedules (the hub's schedules: when a kit
task runs for a repository).

  auto_tasks.py tick     start each scheduled task that is due, then exit

The machine-setup extra `kit-automatic-tasks` (kitlib.machinesetup.extras) runs `tick` every 15
minutes with no window. A tick reads the hub's schedules, starts each one that is due from the
fixed task table (kitlib.autotasks.tick TASKS: the hub names a task, never a program), detached, and
records the start first, under a lock, so two overlapping ticks start nothing twice. It prints one
line per thing it did or skipped; the same lines go to tick.log in the state folder.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.autotasks.tick, prints and
exits. Exit codes: 0 done, nothing due, or the hub not reachable (one line, nothing started); 1 a
hub setting, answer, the state or the lock could not be used, or a program could not be started;
2 usage.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.autotasks import tick                                   # noqa: E402

USAGE = 2
TEXT = 'usage: auto_tasks.py tick'


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args != ['tick']:
        print(TEXT, file=sys.stderr)
        return USAGE
    code, lines = tick.tick()
    for line in lines:
        print(line)
    return code


if __name__ == '__main__':
    sys.exit(main())
