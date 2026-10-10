"""pm_data.py: make or re-link the PM-data branch of a kit repository (docs/pm-data-branch.md
"Making the branch").

  pm_data.py [-C DIR] init --path DATA_DIR [--branch pmdata] [--remote private] [--url URL]
  pm_data.py [-C DIR] init --adopt --path DATA_DIR [--branch NAME] [--remote NAME] [--url URL]

-C names the main checkout (default: here). A relative --path is taken from the current folder.
A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.pmdata.init, prints and
exits. Exit codes: 0 done; 1 refused (nothing changed); 2 usage, or a configuration that cannot be
read; 3 made locally, the push failed; 4 stopped part-way (run again with --adopt); 5 crashed
(an error escaped kitlib.pmdata.init: what was made is not known); 130 Ctrl+C (the lines made so
far are printed).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import pmdata                                           # noqa: E402
from kitlib.pmdata import init as pminit                            # noqa: E402

USAGE = 2
CRASHED = 5


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2 (a usage error is never worded as a refusal)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='pm_data.py', description='Make or re-link the PM-data branch.')
    p.add_argument('-C', dest='root', default='.', help='the main checkout (default: here)')
    sub = p.add_subparsers(dest='verb', metavar='init', parser_class=Parser)
    sub.required = True
    i = sub.add_parser('init', help='make the data branch, its locked worktree and its config')
    i.add_argument('--path', required=True, help='the data worktree folder (absent or empty)')
    i.add_argument('--branch', default=pmdata.DEFAULT_BRANCH,
                   help='the data branch (default: %s)' % pmdata.DEFAULT_BRANCH)
    i.add_argument('--remote', default=pmdata.DEFAULT_REMOTE,
                   help='the private remote name (default: %s)' % pmdata.DEFAULT_REMOTE)
    i.add_argument('--url', help='the private remote url, added when the remote does not exist')
    i.add_argument('--adopt', action='store_true', help='link an existing data branch')
    return p


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    for name in ('path', 'branch', 'remote'):
        value = getattr(a, name)
        if not value.strip() or value.startswith('-'):
            p.error('--%s needs a value' % name)
    if not os.path.isdir(a.root):
        p.error('-C: %s is not a folder' % a.root)
    safe_stdout()
    try:
        outcome = pminit.init(os.path.abspath(a.root), os.path.abspath(a.path), a.branch,
                              a.remote, a.url, a.adopt)
        for line in outcome.lines:
            print(line, flush=True)
    except KeyboardInterrupt:
        return 130
    except Exception as e:      # never a bare traceback whose exit 1 reads as REFUSED
        print('crashed: %s: %s; what was made before the error is not known; run init --adopt '
              'to check' % (type(e).__name__, e), flush=True)
        return CRASHED
    return outcome.code


def safe_stdout():
    """A path the console code page cannot spell (a cp1252 redirect) prints replaced, not raises
    after init has made everything (as tools/machine_setup.py)."""
    try:
        sys.stdout.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass


if __name__ == '__main__':
    sys.exit(main())
