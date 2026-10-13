"""Print a local-workflow proposal. Execution requires controller-bound hosts.
A thin entry of the Codex shell (hosts/codex/README.md); it boots the kit first through the plugin's copy of boot/kitboot.py.
"""
import argparse
import json
import os
from pathlib import Path
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', '..', '..', '..')                      # the kit checkout, when the plugin sits in one
sys.path.insert(0, os.path.join(HERE, '..', 'boot'))                 # the plugin's own copy of boot/kitboot.py
import kitboot                                                       # noqa: E402
kitboot.boot(here=os.path.join(SRC, 'boot', 'kitboot.py')            # the kit, checked, before kitlib
             if os.path.isfile(os.path.join(SRC, 'kit.json')) else None)
import kitlib  # noqa: E402,F401  (after the boot check: installs the CreateProcess hook)
from hosts.codex.adapter.workflow import describe, plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for name in ('cwd', 'branch', 'item', 'session', 'model'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--path', action='append', required=True)
    args = parser.parse_args(argv)
    try:
        result = plan(args.cwd, args.branch, args.item, args.session, args.model, args.path)
    except (ValueError, RuntimeError) as exc:
        print('REFUSED: ' + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(describe(result), indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
