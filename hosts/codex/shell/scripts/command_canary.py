"""Run the private native kit-command/child-role canary without model inference.
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--codex-exe')
    parser.add_argument('--fixture-root', required=True, help='reviewed disposable fixture directory outside system TEMP')
    args = parser.parse_args()
    try:
        from hosts.codex.adapter.command_canary import probe
        print(json.dumps(probe(args.codex_exe, fixture_root=args.fixture_root), indent=2))
        return 0
    except Exception as error:
        sys.stderr.write('Native kit-command/role binding unverified: %s: %s\n' % (type(error).__name__, error))
        return 2


if __name__ == '__main__':
    sys.exit(main())
