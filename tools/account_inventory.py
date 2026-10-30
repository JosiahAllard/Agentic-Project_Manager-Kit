"""account_inventory.py: how far each Claude account folder has drifted from the shared setup
(docs/machine-setup.md "Account inventory"). Read only: it changes nothing and removes nothing; the
removals it prints are for the owner to run by hand.

  account_inventory.py          plain lines per account folder, then a summary
  account_inventory.py --json   the same as one JSON object
  options: --kit DIR (default: the checkout this file is in)   --home DIR (default: the user's home)
           --export-dir DIR (default: the machine config's plugin export)

A thin entry point (ARCHITECTURE.md, Layers). Exit code 0 whenever the tool ran, whatever it found;
2 usage.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.config import user                                      # noqa: E402
from kitlib.machinesetup import inventory                           # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(prog='account_inventory.py', description=__doc__.split('\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--kit', default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p.add_argument('--home', default=None)
    p.add_argument('--export-dir', default=None)
    p.add_argument('--json', action='store_true')
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    try:
        sys.stdout.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass
    res = inventory.inventory(a.home or user.home_dir(), a.kit, export_dir=a.export_dir)
    if a.json:
        print(json.dumps(res, indent=2, sort_keys=True))
    else:
        for line in inventory.lines(res):
            print(line)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
