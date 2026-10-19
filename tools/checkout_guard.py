"""checkout_guard.py: is a path in the MAIN checkout of its repository, and has anything strayed
into the main checkout (docs/preflight.md)?

  checkout_guard.py [PATH]
      prints `main`, `linked`, `none` (no git work tree) or `unknown` (git could not say) and the
      work tree's top level. Exit 0 linked or none, 1 main, 2 unknown (or a usage error).
  checkout_guard.py --main-stray [--allow-tracked] [PATH]
      the main checkout's stray untracked or uncommitted files, found from PATH (default: here),
      one [FAIL] line each (kitlib.git.stray). Exit 1 on any; 0 when clean or when git
      could not tell (a WARNING line). --allow-tracked is for a repo's landing gate step (it
      runs in the _landing linked worktree): there "could not tell" fails (exit 1). It exempts
      no stray; the name is kept for the merged_args repos already pass.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.git, prints and exits. The
write tools' refusal is kitlib.git.checkout.main_refusal.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.git import repo, stray                                 # noqa: E402

USAGE = 2
CODES = {repo.MAIN: 1, repo.UNKNOWN: 2}


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def build_parser():
    p = Parser(prog='checkout_guard.py',
               description='Main checkout or linked worktree; or the main checkout\'s strays.')
    p.add_argument('path', nargs='?', default='.', help='the path asked about (default: here)')
    p.add_argument('--main-stray', action='store_true',
                   help='list stray changes in the main checkout (exit 1 on any)')
    p.add_argument('--allow-tracked', action='store_true',
                   help='with --main-stray, for a landing gate step: fail, not warn, when git '
                        'could not tell')
    return p


def print_safe(text):
    """print(text), never raising UnicodeEncodeError for a path the console's codepage cannot
    encode: the offending characters are replaced and the line still prints."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, 'encoding', None) or 'ascii'
        print(text.encode(enc, errors='replace').decode(enc, errors='replace'), flush=True)


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.allow_tracked and not a.main_stray:
        build_parser().error('--allow-tracked goes with --main-stray')
    if a.main_stray:
        code, lines = stray.check(a.path, allow_tracked=a.allow_tracked)
        for line in lines:
            print_safe(line)
        return code
    place = repo.classify(a.path)
    print_safe('%-8s %s' % (place.kind.lower(), place.top or place.detail))
    return CODES.get(place.kind, 0)


if __name__ == '__main__':
    sys.exit(main())
