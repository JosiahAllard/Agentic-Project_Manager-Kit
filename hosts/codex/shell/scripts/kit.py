"""Thin role-aware entry to existing kit tools; --plan prints argv without execution.
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
    parser.add_argument("tool")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--review-verdict", choices=("LAND", "LAND WITH FOLLOW-UP"))
    # Split the shared tool's arguments so its own CLI remains the argument authority.
    front, tail = sys.argv[1:], []
    if "--" in front:
        index = front.index("--")
        front, tail = front[:index], front[index + 1:]
    options = parser.parse_args(front)
    try:
        from hosts.codex.adapter.dispatch import execute, plan
        planned = plan(options.tool, tail, options.cwd, review_verdict=options.review_verdict)
        if options.plan:
            print(json.dumps({"argv": planned.argv, "cwd": planned.cwd, "role": planned.role}))
            return 0
        return execute(planned)
    except (ValueError, RuntimeError) as error:
        sys.stderr.write("Codex kit operation refused: %s\n" % error)
        return 2


if __name__ == "__main__":
    sys.exit(main())
