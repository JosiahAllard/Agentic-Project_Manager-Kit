"""Inspect native tools locally without inference; optional canary checks method types only.
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
