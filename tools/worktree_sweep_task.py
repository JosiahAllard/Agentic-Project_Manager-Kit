"""worktree_sweep_task.py: the Windows scheduled task for the nightly worktree sweep
(docs/worktree-remove.md). The owner runs it once.

  worktree_sweep_task.py [-C <main checkout>]                              print the registration
  worktree_sweep_task.py [-C <main checkout>] --register --owner-confirmed print it, then run it

`--register` without `--owner-confirmed` is refused (exit 1) and registers nothing; agents never
type `--owner-confirmed`. The task runs this kit's tools/worktree_remove.py by absolute path, which
must be in the kit's main checkout. A thin entry point (ARCHITECTURE.md, Layers): it parses, calls
kitlib.worktree.task, prints and exits. Exit codes: 0 printed or registered; 1 refused, or
PowerShell failed; 2 usage; 130 Ctrl+C.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from kitlib.worktree import task                                    # noqa: E402


def main(argv=None, runner=subprocess.call, root=None, tool=None):
    """`runner`, `root` and `tool` are seams for tests (a fake PowerShell, a temporary repository,
    a tool in a temporary main checkout)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        outcome = task.run(argv, runner, root or os.getcwd(),
                           tool or os.path.join(HERE, 'worktree_remove.py'))
    except KeyboardInterrupt:
        return 130
    stream = sys.stderr if outcome.code == task.USAGE else sys.stdout
    if outcome.code == task.USAGE:
        stream.write(__doc__)
    for line in outcome.lines:
        print(line, file=stream, flush=True)
    return outcome.code


if __name__ == '__main__':
    sys.exit(main())
