"""kit_stubs.py: add a repository's agent memory folder to its .claude/settings.local.json
(docs/install.md).

  kit_stubs.py memory [-C DIR] [--apply]

-C names the checkout (default: here). A thin entry point (ARCHITECTURE.md, Layers): it parses,
calls kitlib.stubs.memory.run (a dry run unless --apply), prints and exits. Exit codes: 0 done or
current; 1 a problem named (a settings file it cannot edit, or a failed write); 2 usage, or the
kit configuration was refused (a hosts.json it cannot read is one `kit_stubs: ERROR:` line); 130
Ctrl+C.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.git import repo as gitrepo                              # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2 (a usage error is never worded as a refusal)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='kit_stubs.py', description="Add a repository's agent memory folder key.")
    p.add_argument('verb', choices=('memory',))
    p.add_argument('-C', dest='root', default='.', help='the checkout (default: here)')
    p.add_argument('--apply', action='store_true',
                   help='write the key (without it memory is a dry run)')
    return p


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    top, why = gitrepo.toplevel(a.root)
    if top is None:
        p.error('-C: %s' % why)
    from kitlib.host import registry
    from kitlib.stubs import memory
    try:
        outcome = memory.run(top, apply=a.apply)
    except KeyboardInterrupt:
        return 130
    except registry.RegistryError as e:          # hosts.json refused: the kit configuration
        print('kit_stubs: ERROR: %s' % e, file=sys.stderr, flush=True)
        return USAGE
    for line in outcome.lines:
        print(line, flush=True)
    return outcome.code


if __name__ == '__main__':
    sys.exit(main())
