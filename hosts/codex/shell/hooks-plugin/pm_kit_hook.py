"""pm-kit-hooks: the PM kit's Codex hooks. Each runs boot/hook.py of the pm-kit plugin Codex
installed beside this one (<Codex plugin cache>/pm-kit/pm-kit/<version>), with the same arguments,
input and exit code. With no such kit, a tool event is blocked (exit 2) and a start event says so
(exit 0).

Codex asks the owner to trust these hooks again when any byte of this plugin changes, so this
folder never changes with the kit (docs/plugin-source.md "Codex hooks plugin"). It imports nothing
from beside it, so nothing is written here.
"""
import os
import re
import runpy
import sys

STARTS = ('SessionStart', 'PostCompact', 'SubagentStart')
BLOCK = 2
FIX = 'reinstall the kit: codex plugin add pm-kit@pm-kit (with Codex closed)'


def order(name):
    """A version folder name as a sort key: digit runs by number, other runs as text."""
    return [(0, int(p), '') if p.isdigit() else (1, 0, p) for p in re.findall(r'\d+|\D+', name)]


def kit_hook(here):
    """boot/hook.py of the newest pm-kit version folder beside this plugin (by version, then
    modified time); LookupError when there is none."""
    top = os.path.join(os.path.dirname(os.path.dirname(here)), 'pm-kit')
    found = []
    for name in os.listdir(top):
        hook = os.path.join(top, name, 'boot', 'hook.py')
        if os.path.isfile(hook):
            found.append((order(name), os.path.getmtime(os.path.join(top, name)), hook))
    if not found:
        raise LookupError('no pm-kit plugin with boot/hook.py in %s' % top)
    return max(found)[2]


def main(argv):
    event = argv[2] if len(argv) > 2 else ''
    try:
        hook = kit_hook(os.path.dirname(os.path.abspath(__file__)))
        sys.argv = [hook] + list(argv[1:])
        sys.path[0] = os.path.dirname(hook)
        runpy.run_path(hook, run_name='__main__')
        raise RuntimeError('%s ended without an exit code' % hook)
    except SystemExit:
        raise
    except BaseException as e:
        start = event in STARTS
        try:
            if start:
                sys.stdout.write('PM kit: the kit did not load: %s: %s; %s\n' % (type(e).__name__, e, FIX))
            else:
                sys.stderr.write('kit hook: blocked, the kit did not load: %s: %s; %s\n' % (type(e).__name__, e, FIX))
        except BaseException:
            pass
        return 0 if start else BLOCK


if __name__ == '__main__':
    sys.exit(main(sys.argv))
