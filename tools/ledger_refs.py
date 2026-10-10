"""ledger_refs.py: where is each ruling id cited? Run it before retiring ledger entries, so a
citation is never broken silently (docs/ledger.md "ledger_refs").

  ledger_refs.py R-nnnn [R-nnnn ...]
  ledger_refs.py --from-json FILE [FILE ...]     each FILE a JSON list of objects with an "id"

One block per id, in id order: `R-nnnn  refs=N  ledger-mentions=M`, then `  place:file:line` per
hit. The places are the machine config's `ledger_refs` (kitlib.config.user); the mentions count
the ledger file's lines naming the id, less its own header.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.ledgerrefs, prints and
exits. Exit codes: 0 every place searched; 1 a place could not be searched (an `ERROR` line names
it; the hits found elsewhere are still printed) or the machine config is broken; 2 usage error.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import ledgerrefs                                      # noqa: E402
from kitlib.config import user                                     # noqa: E402
from kitlib.config.types import ConfigError                        # noqa: E402


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(2)


def parser():
    p = Parser(prog='ledger_refs.py', description='Where each ruling id is cited.')
    p.add_argument('ids', nargs='*', metavar='R-nnnn')
    p.add_argument('--from-json', nargs='+', metavar='FILE', help='JSON lists of {"id": ...} objects')
    return p


def ids_from(a, p):
    ids = list(a.ids)
    for path in a.from_json or []:
        try:
            with open(path, encoding='utf-8-sig') as f:
                ids += [e['id'] for e in json.load(f)]
        except (OSError, ValueError, TypeError, KeyError) as e:
            p.error('cannot read ids from %s: %s' % (path, e))
    if not ids:
        p.error('name at least one ruling id, or --from-json')
    bad = [i for i in ids if not ledgerrefs.ID_RE.match(str(i))]
    if bad:
        p.error('not a ruling id R-nnnn: %s' % ', '.join(map(str, bad)))
    return sorted(set(ids))


def main(argv=None):
    p = parser()
    ids = ids_from(p.parse_args(argv), p)
    try:
        cfg = user.user_config()
    except ConfigError as e:
        sys.stderr.write('ledger_refs.py: %s\n' % e)
        return 1
    failed = False
    for rid in ids:
        r = ledgerrefs.find(rid, cfg['ledger_refs'], cfg['ledger_path'])
        print('%s  refs=%d  ledger-mentions=%d' % (r.id, len(r.hits), r.ledger_mentions))
        for h in r.hits:
            print('  ' + h)
        for e in r.errors:
            print('  ERROR ' + e)
            failed = True
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
