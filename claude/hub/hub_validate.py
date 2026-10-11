"""hub_validate.py: checks Action Hub data before it is written (docs/action-hub.md "For the PM").

  hub_validate.py --dir DIR
      DIR holds <collection>/<doc_id>.json (an ArtifactData out_dir, or the mirror's hub/ folder);
      every mirrored collection in it is checked, repos/ too when present
  hub_validate.py --batch FILE [--against DIR]
      FILE is an ArtifactData batch (a list of entries) about to be sent: its `set` documents are
      checked. Dangling refs and repo tags are judged against DIR (the hub as last read) plus the
      batch; without --against they are not judged, and a line says so

Prints one line per finding and a count. Exit 0: no errors (warnings alone are 0, 1 with --strict);
1: errors; 2: usage, or an unreadable folder or batch.
A thin entry point: kitlib.hubvalidate does the work, kitlib.hubmirror reads the folders.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from kitlib import hubmirror, hubvalidate as V                      # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check Action Hub data against its field tables.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dir", help="an out_dir or mirror folder of <collection>/<doc_id>.json")
    g.add_argument("--batch", help="an ArtifactData batch file about to be sent")
    ap.add_argument("--against", help="with --batch: the hub as last read, to judge refs and repos")
    ap.add_argument("--strict", action="store_true", help="warnings also exit 1")
    a = ap.parse_args(argv)
    if a.against and not a.batch:
        ap.error("--against goes with --batch")
    try:
        if a.dir:
            docs, _notes = hubmirror.read_source(a.dir)
            repos = hubmirror.read_repos(a.dir)
            if repos is not None:
                docs["repos"] = repos
            findings = V.validate(docs)
        else:
            with open(a.batch, encoding="utf-8-sig") as f:
                entries = json.load(f)
            docs, findings = V.batch_docs(entries, os.path.dirname(os.path.abspath(a.batch)))
            only = {"%s/%s" % (c, i) for c in docs for i in docs[c]}
            if a.against:
                base, _notes = hubmirror.read_source(a.against)
                repos = hubmirror.read_repos(a.against)
                if repos is not None:
                    base["repos"] = repos
                for c, d in docs.items():
                    base.setdefault(c, {}).update(d)
                docs = base
                findings = findings + V.validate(docs, only=only)
            else:
                findings = findings + [
                    f for f in V.validate(docs, only=only)
                    if f.code not in ("ref-dangling", "repo-alias")]
                findings.append(V.Finding(V.NOTE, "", "", "ref-dangling",
                                          "refs and repo tags not judged: no --against"))
    except hubmirror.Refused as e:
        print("REFUSED: %s" % e, file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print("cannot read the input: %s" % e, file=sys.stderr)
        return 2
    for f in findings:
        print(V.format_line(f))
    print(V.summary(findings))
    errors = any(f.level == V.ERROR for f in findings)
    warns = any(f.level == V.WARN for f in findings)
    return 1 if errors or (a.strict and warns) else 0


if __name__ == "__main__":
    sys.exit(main())
