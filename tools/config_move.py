"""config_move.py: move the machine config from the home folder into the PM repository
(docs/kit-json.md "Moving the machine config into the PM repo").

  config_move.py
      a dry run: what would be written, dropped, left behind or refused; writes nothing
  config_move.py --apply
      copy into <PM repo>/.pm-kit and check the copy; never deletes, commits or pushes, and
      prints the owner's commands for those

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.config.move, prints and
exits. Exit codes: 0 OK (warn lines included), 1 REFUSED, 2 USAGE.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.config import move                                      # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='config_move.py', allow_abbrev=False,
               description='Move the machine config into the PM repository.')
    p.add_argument('--apply', action='store_true',
                   help='copy the files (default: a dry run that writes nothing)')
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    code, lines = move.run(apply_it=a.apply)
    for line in lines:
        print(line, flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
