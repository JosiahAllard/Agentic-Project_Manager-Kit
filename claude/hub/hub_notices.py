"""hub_notices.py: posts the machine lock's lock-window notices to the Action Hub inbox
(docs/action-hub.md "Lock-window notices").

  hub_notices.py [--dir DIR] [--out PATH]
      print every notice not yet posted, one per line; with --out, also write an ArtifactData
      batch that sets each one as inbox/<its id>
  hub_notices.py [--dir DIR] --posted PATH
      after the batch at PATH was applied: move the notices it set into DIR/posted/, so neither
      this tool nor the PM watcher shows them again

--dir defaults to machine_lock.notice.dir of the machine config; neither: exit 2.
A thin entry point: kitlib.pmwatch does the work.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from kitlib import pmwatch as W                                     # noqa: E402


def main(argv=None, config_path=None):
    ap = argparse.ArgumentParser(description="Post the machine lock's lock-window notices.")
    ap.add_argument("--dir", help="the notice folder (default: machine_lock.notice.dir)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--out", help="write an ArtifactData batch of the pending notices here")
    g.add_argument("--posted", help="a batch this tool wrote and that was applied: mark it posted")
    a = ap.parse_args(argv)
    d = a.dir or W.configured_notice_dir(config_path)
    if not d:
        print("no --dir and no machine_lock.notice.dir in the machine config", file=sys.stderr)
        return 2
    if a.posted:
        try:
            with open(a.posted, encoding="utf-8") as f:
                writes = json.load(f)
            names = [W.notice_file(w.get("doc_id")) for w in writes
                     if isinstance(w, dict) and w.get("collection") == "inbox"]
        except (OSError, ValueError, AttributeError, TypeError) as e:
            print("cannot read the batch %s: %s" % (a.posted, e), file=sys.stderr)
            return 2
        moved = W.mark_posted(d, [n for n in names if n])
        print("%d marked posted" % len(moved))
        return 0
    pending = W.pending_notices(d)
    for n, doc in pending:
        print("%s %s" % (doc["id"], " ".join(str(doc.get("title") or "").split())))
    if a.out:
        writes = W.notice_batch([doc for _n, doc in pending])
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(writes, f, indent=1)
        print("%d to post -> %s" % (len(writes), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
