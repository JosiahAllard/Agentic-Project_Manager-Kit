"""commit.py: stage explicit paths and commit them with the right trailers (docs/commit.md).

  commit.py [-C DIR] -m SUBJECT [-b BODY | --body-file FILE] [--model NAME]
            [--owner-authored] [--owner-approved VALUE ...] [--lander-main-checkout] PATH ...
      commit exactly PATH ... in the work tree DIR is in (default: here)

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.commit, prints and exits.
Exit codes (kitlib.commit): 0 committed, 1 refused or git failed, 2 USAGE (also the main-checkout
refusal and a refused .kit.json).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import commit as C                                      # noqa: E402
from kitlib.git import repo                                         # noqa: E402


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(C.USAGE)


class OneSubject(argparse.Action):
    """-m once; a second -m would silently replace the first, so it is a usage error."""

    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error('one -m (the subject); put the body in -b or --body-file')
        setattr(namespace, self.dest, values)


def parser():
    p = Parser(prog='commit.py', allow_abbrev=False,
               description='Stage explicit paths and commit them with the right trailers.')
    p.add_argument('-C', dest='root', default='.',
                   help='a path in the work tree to commit in (default: here)')
    p.add_argument('-m', dest='subject', required=True, action=OneSubject,
                   help='the subject line (once; the body goes in -b or --body-file)')
    body = p.add_mutually_exclusive_group()
    body.add_argument('-b', dest='b', default='',
                      help='the body TEXT (a typed \n becomes a newline; ~~ also becomes a '
                           'newline; for a literal ~~ use --body-file)')
    body.add_argument('--body-file', dest='body_file', default=None,
                      help='read the body from this file (UTF-8)')
    p.add_argument('--model', default=None,
                   help='the model you run on (default: CLAUDE_AGENT_MODEL)')
    p.add_argument('--provider', choices=('claude', 'codex'), default='claude',
                   help='agent provider (default: claude); Codex requires --model')
    p.add_argument('--owner-authored', action='store_true',
                   help="the owner's own commit: Owner-Authored: yes, no Co-Authored-By")
    p.add_argument('--owner-approved', action='append', default=[],
                   help='an owner-only path or blanket entry the owner approved (repeatable)')
    p.add_argument('--lander-main-checkout', action='store_true',
                   help='a lander commit in the main checkout (refused anywhere else)')
    p.add_argument('paths', nargs='+', help='the files to commit, each named')
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    root, why = repo.toplevel(os.path.abspath(a.root))
    if root is None:
        print('USAGE: -C %s is not in a git work tree: %s' % (a.root, why), flush=True)
        return C.USAGE
    out = C.commit(C.request(root, a.subject, a.paths, b=a.b, body_file=a.body_file,
                             model=a.model, owner_authored=a.owner_authored,
                             owner_approved=a.owner_approved,
                             lander_main_checkout=a.lander_main_checkout, provider=a.provider))
    for line in out.lines:
        print(line, flush=True)
    return out.code


if __name__ == '__main__':
    sys.exit(main())
