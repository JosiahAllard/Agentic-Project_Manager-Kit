"""hub_repos.py: the Action Hub's repo list (docs/action-hub.md "repos/<name>").

  hub_repos.py [--have NAME ...] [--out PATH]
      print every repository the PM knows, one per line; with --out, also write an ArtifactData
      batch that creates repos/<name> for each one not named by --have (the names already stored)

A thin entry point: kitlib.pmstart.known_repos does the work from the user-profile config.
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from kitlib import pmstart                                          # noqa: E402
from kitlib.config import ConfigError                              # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="The Action Hub's repo list.")
    ap.add_argument("--have", nargs="*", default=[], help="repo names already in repos/")
    ap.add_argument("--out", help="write an ArtifactData batch for the missing ones here")
    a = ap.parse_args(argv)
    try:
        cfg = pmstart.load_config()
    except ConfigError as e:
        print("machine config: %s" % e, file=sys.stderr)
        return 2
    if cfg is None:
        print("no pm_start config", file=sys.stderr)
        return 2
    names = pmstart.known_repos(cfg)
    for n in names:
        print(n)
    if a.out:
        have = {h.lower() for h in a.have}
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        writes = [{"op": "set", "collection": "repos", "doc_id": n,
                   "data": {"name": n, "added_at": now}} for n in names if n.lower() not in have]
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(writes, f, indent=1)
        print("%d to add -> %s" % (len(writes), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
