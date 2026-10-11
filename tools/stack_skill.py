"""stack_skill.py: copy a stack's skills into a repository with the kit's path written in (docs/install.md).

  stack_skill.py install --stack NAME [-C DIR] [--apply]

Dry run by default: says what it would create. --apply writes. A file already in the repository is
kept. The kit path is this kit's own, never taken from the caller. A thin entry point
(ARCHITECTURE.md, Layers). Exit codes: 0 done or dry run printed; 1 refused (nothing written);
2 usage; 130 Ctrl+C.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import stackskill                                       # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='stack_skill.py', description="Copy a stack's skills into a repository.")
    p.add_argument('verb', choices=('install',))
    p.add_argument('--stack', required=True, help='the stack name (templates/stacks/<name>)')
    p.add_argument('-C', dest='root', default='.', help='the repository (default: here)')
    p.add_argument('--apply', action='store_true', help='write the files (default: dry run)')
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        parser().error('-C: %s is not a folder' % root)
    written = []
    try:
        lines = stackskill.install(a.stack, root, apply=a.apply, written=written)
    except stackskill.StackSkillRefused as e:
        print('stack_skill: REFUSED - %s; nothing was written' % e, flush=True)
        return 1
    except OSError as e:
        for w in written:
            print('wrote    %s' % w, flush=True)
        print('stack_skill: FAILED - %s; %s' % (e, 'already written (delete to undo): %s' % ' , '.join(written)
                                                 if written else 'nothing had been written before it'), flush=True)
        return 1
    except KeyboardInterrupt:
        return 130
    for line in lines:
        print(line, flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
