"""new_worktree.py: create a workstream's worktree in one call (docs/new-worktree.md).

  new_worktree.py [-C DIR] <area>/<slug>                   a NEW branch off the main branch
  new_worktree.py [-C DIR] --existing <area>/<slug>        an EXISTING branch, resumed
                  [--base BRANCH]                          another base than the main branch
                  [--no-wait]                              do not wait for a landing in flight
                  [--host NAME]                            that host's worktree folder

-C names the repository by any of its work trees (default: here); the new worktree is always the
MAIN checkout's. A thin entry point (ARCHITECTURE.md, Layers): it parses, calls
kitlib.worktree.new, prints and exits. Exit codes: 0 made; 1 refused (nothing made); 2 usage, or
the kit configuration was refused; 130 Ctrl+C.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.git import repo as gitrepo                              # noqa: E402
from kitlib.host import registry                                    # noqa: E402
from kitlib.worktree import new                                     # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2 (a usage error is never worded as a refusal)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='new_worktree.py', description='Create a worktree for a new or existing branch.')
    p.add_argument('branch', help='<area>/<slug>')
    p.add_argument('-C', dest='root', default='.', help='any work tree of the repository (default: here)')
    p.add_argument('--existing', action='store_true', help='resume a branch that exists')
    p.add_argument('--base', help='the base branch (default: "repo".main_branch)')
    p.add_argument('--host', default=None,
                   help='the agent host whose worktree folder to use (default: $PM_KIT_HOST, else '
                        'the hosts.json default, whose folder is "repo".worktrees_dir)')
    p.add_argument('--no-wait', dest='wait', action='store_false',
                   help='do not wait for a landing in flight ("landing".in_flight); say so and go on')
    return p


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    if not new.branch_ok(a.branch, a.existing):
        p.error('%r is not a %s branch name: <area>/<slug>, ASCII letters, digits, _ . -, each part '
                'starting with a %s' % (a.branch, 'resumable' if a.existing else 'new',
                                        'letter or digit (area lowercase)' if a.existing
                                        else 'lowercase letter or digit'))
    if a.base is not None and (not a.base.strip() or a.base.startswith('-')):
        p.error('--base needs a branch name')
    try:
        host = registry.choose(a.host)
    except registry.RegistryError as e:
        p.error('--host: %s' % e)
    top, why = gitrepo.toplevel(a.root)
    if top is None:
        p.error('-C: %s' % why)
    try:
        outcome = new.create(top, a.branch, existing=a.existing, base=a.base, wait=a.wait,
                             say=lambda line: print(line, flush=True), host=host)
    except KeyboardInterrupt:
        return 130
    for line in outcome.lines:
        print(line, flush=True)
    return outcome.code


if __name__ == '__main__':
    sys.exit(main())
