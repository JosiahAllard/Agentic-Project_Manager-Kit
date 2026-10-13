"""A retired hook path (docs/machine-setup.md "Moved hook paths"). Settings written before the hosts/
move (multi-host plan step 7) run <kit>/claude/hooks/<this file>; a missing guard would block every
tool call, and a missing session hook would drop its checklist. So this runs the same-named entry in
hosts/claude/shell/hooks/ in its place until tools/machine_setup.py has re-pointed each settings
entry. Delete claude/ one release after the move. Exit 2 (a block) when the entry cannot be run.
"""
import os, runpy, sys
NEW = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   'hosts', 'claude', 'shell', 'hooks', os.path.basename(__file__))

if __name__ == '__main__':
    try:
        sys.argv[0] = NEW
        runpy.run_path(NEW, run_name='__main__')
    except SystemExit:
        raise
    except BaseException:
        sys.exit(2)
