"""Preview a dedicated role session. Execution is disabled pending activation review.
A thin entry of the Codex shell (hosts/codex/README.md); it boots the kit first through boot/kitboot.py of the export or checkout it sits in.
"""
import argparse
import json
import os
from pathlib import Path
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', '..', '..', 'boot'))  # the kit's own boot/kitboot.py
import kitboot                                                       # noqa: E402
kitboot.boot()                                                       # the kit, checked, before kitlib
import kitlib  # noqa: E402,F401  (after the boot check: installs the CreateProcess hook)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for name in ("role", "cwd", "name", "brief"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--codex-exe")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        from hosts.codex.adapter.launcher import execute, plan
        planned = plan(args.role, args.cwd, args.name, args.brief, model=args.model, effort=args.effort,
                       codex_exe=args.codex_exe)
        if args.execute:
            return execute(planned)
        print(json.dumps({"argv": planned.argv, "cwd": planned.cwd, "environment": planned.environment,
                          "role": planned.role, "name": planned.name, "prompt": planned.prompt}))
        return 0
    except (ValueError, OSError) as error:
        sys.stderr.write("Codex role launch refused: %s\n" % error)
        return 2


if __name__ == "__main__":
    sys.exit(main())
