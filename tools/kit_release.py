"""Prepare or check an immutable shared runtime; never activate a plugin or service."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

from kitlib import kitrelease as R


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export', allow_abbrev=False)
    export.add_argument('--source', required=True)
    export.add_argument('--commit', required=True)
    export.add_argument('--destination', required=True)
    check = commands.add_parser('verify', allow_abbrev=False)
    check.add_argument('--root', required=True)
    check.add_argument('--identity', required=True)
    check.add_argument('--commit')
    args = parser.parse_args(argv)
    try:
        if args.command == 'export':
            result = R.export(args.source, args.commit, args.destination)
        else:
            result = R.verify(args.root, args.identity, args.commit)
    except (ValueError, OSError) as error:
        print('kit_release: refused: ' + str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
