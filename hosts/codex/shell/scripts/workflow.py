"""Print a local-workflow proposal. Execution requires controller-bound hosts.
A thin entry of the Codex shell (hosts/codex/README.md); it boots the kit first (boot/kitboot.py).
"""
import argparse
import json
import os
from pathlib import Path
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BOOT = (os.path.join(HERE, '..', '..', '..', '..', 'boot'), os.path.join(os.environ.get('PM_KIT_ROOT') or HERE, 'boot'))
sys.path.insert(0, next((b for b in BOOT if os.path.isfile(os.path.join(b, 'kitboot.py'))), BOOT[1]))
import kitboot                                                       # noqa: E402
kitboot.boot()                                                       # the kit, checked, before kitlib
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
