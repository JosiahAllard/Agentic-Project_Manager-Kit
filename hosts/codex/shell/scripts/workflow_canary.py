"""Private actual-native local workflow acceptance without inference or installation.
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--codex-exe')
    parser.add_argument('--fixture-root', required=True)
    args = parser.parse_args()
    try:
        from hosts.codex.adapter.workflow_canary import probe
        result = probe(args.codex_exe, fixture_root=args.fixture_root)
        print(json.dumps(result, indent=2))
        return 0 if result['complete'] else 2
    except Exception as error:
        sys.stderr.write('Native local workflow unverified: %s: %s\n' % (type(error).__name__, error))
        return 2


if __name__ == '__main__':
    sys.exit(main())
