"""Inspect native tools locally without inference; optional canary checks method types only.
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
    parser.add_argument("--codex-exe")
    parser.add_argument("--model", default="pmkit-offline-probe", help="model name selects local tool metadata only")
    parser.add_argument("--canary", action="store_true", help="also run fixed typeof checks in native code mode; no shell/file operations")
    args = parser.parse_args()
    try:
        from hosts.codex.adapter.runtime_probe import probe
        print(json.dumps(probe(args.codex_exe, args.model, canary=args.canary), indent=2))
        return 0
    except (ValueError, RuntimeError, OSError) as error:
        sys.stderr.write("Native command isolation unverified: %s\n" % error)
        return 2


if __name__ == "__main__":
    sys.exit(main())
