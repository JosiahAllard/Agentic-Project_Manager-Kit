"""hub_backlog.py: fill the Action Hub's Current backlog and Completed tabs (docs/action-hub.md).

  hub_backlog.py --out PATH [--force] [--state PATH]
      for every repository the PM knows (kitlib.pmstart.known_repos) that has a folder, snapshot its
      published backlog records (its .kit.json store; kitlib.hubbacklog.store_at) and write an
      ArtifactData batch setting backlog/<repo> and completed/<repo> for each document that changed since the last run (all of them with --force).
      Each document goes to <out without extension>.docs/<collection>-<repo>.json and the batch
      entry names it by file_path.
      Prints one line per repository and the number of writes; no writes means nothing to send.

A repository's folder is pm_start.repo_paths[<name>] or the PM repository's sibling of that name.
The state file remembers each document's digest and store version as of the last batch, and pins
every write to that version. If a batch was never sent, or a write is refused for its version, read
the collection, fix the versions in the state file, and rerun with --force.
The state file is hub_backlog_state.json in the machine config folder (user.DIR_NAME); while that
folder has none, the retired folder's is read and the new one written (docs/kit-json.md "Moving
the machine config folder").
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from kitlib import hubbacklog, pmstart                             # noqa: E402
from kitlib.config import ConfigError, migration, user             # noqa: E402

STATE_NAME = "hub_backlog_state.json"
STATE = "~/%s/%s" % (user.DIR_NAME, STATE_NAME)


def state_paths(given, home=None):
    """(read, write) for the state file: --state for both when given; else the machine config
    folder's, read from the retired folder while the new one has none (kitlib.config.migration
    .state_paths), so the first run after the folder's move keeps every pinned version."""
    if given:
        p = os.path.expanduser(given)
        return p, p
    return migration.state_paths(STATE_NAME, home)


def plan(names, roots, state, now, snap=hubbacklog.snapshot, force=False):
    """(batch writes, new state, report lines) for the repositories in names."""
    writes, new, lines = [], dict(state), []
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for n in names:
        root = roots.get(n)
        if not root:
            lines.append("%s: no folder, skipped" % n)
            continue
        try:
            docs = snap(n, root, now=now)
        except (RuntimeError, OSError) as e:
            lines.append("%s: could not read: %s" % (n, e))
            continue
        changed = []
        for coll, doc in zip(("backlog", "completed"), docs):
            key, d = "%s/%s" % (coll, n), hubbacklog.digest(doc)
            was = state.get(key) or {}
            if force or was.get("d") != d:
                w = {"op": "set", "collection": coll, "doc_id": n, "data": dict(doc, posted_at=stamp)}
                if was.get("v"):
                    w["if_version"] = was["v"]  # the store refuses an unpinned write to an existing document
                writes.append(w)
                new[key] = {"d": d, "v": was.get("v", 0) + 1}
                changed.append(coll)
        lines.append("%s: %d open, %d completed%s" % (n, len(docs[0]["items"]), len(docs[1]["items"]),
                                                      ("; writes " + ", ".join(changed)) if changed else ""))
    return writes, new, lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fill the Action Hub backlog tabs.")
    ap.add_argument("--out", required=True, help="write the ArtifactData batch here")
    ap.add_argument("--force", action="store_true", help="write every document, changed or not")
    ap.add_argument("--state", help="last-written digests (default %s; read from the retired "
                                    "folder's while that one is absent)" % STATE)
    a = ap.parse_args(argv)
    try:
        cfg = pmstart.load_config()
    except ConfigError as e:
        print("machine config: %s" % e, file=sys.stderr)
        return 2
    if cfg is None:
        print("no pm_start config", file=sys.stderr)
        return 2
    pm_root = hubbacklog.git(os.path.dirname(os.path.abspath(cfg["sessions_file"])),
                             "rev-parse", "--show-toplevel").decode("utf-8").strip()
    names = pmstart.known_repos(cfg)
    roots = {n: hubbacklog.repo_root(n, pm_root, cfg.get("repo_paths")) for n in names}
    read_path, state_path = state_paths(a.state)
    try:
        with open(read_path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    writes, new, lines = plan(names, roots, state, datetime.datetime.now(datetime.timezone.utc), force=a.force)
    for line in lines:
        print(line)
    docs_dir = os.path.splitext(os.path.abspath(a.out))[0] + ".docs"
    os.makedirs(docs_dir, exist_ok=True)
    for w in writes:  # each document in its own file, so the batch names files and stays small
        path = os.path.join(docs_dir, "%s-%s.json" % (w["collection"], w["doc_id"]))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(w.pop("data"), f, ensure_ascii=False)
        w["file_path"] = path.replace(os.sep, "/")
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(writes, f, indent=1)
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(new, f, indent=1, sort_keys=True)
    print("%d writes -> %s" % (len(writes), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
