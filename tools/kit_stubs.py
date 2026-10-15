"""kit_stubs.py: write or check a repository's stubs for the kit's tools, or add its agent memory
folder to its .claude/settings.json (docs/install.md).

  kit_stubs.py write [-C DIR] [--tools a,b,...] [--replace] [--dry-run]
  kit_stubs.py check [-C DIR] [--tools a,b,...]
  kit_stubs.py memory [-C DIR] [--apply]

-C names the checkout (default: here). A thin entry point (ARCHITECTURE.md, Layers): it parses,
calls kitlib.stubs.run (memory: kitlib.stubs.memory.run, a dry run unless --apply), prints and
exits. Exit codes: 0 done or all current; 1 a problem named (memory: a settings file it cannot
edit, or a failed write); 2 usage, or the kit configuration was refused (a hosts.json it cannot read
is one `kit_stubs: ERROR:` line), or run from an export of the kit whose marker names no main
checkout (no stub may name a folder a prune may remove); 130 Ctrl+C.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import stubs                                            # noqa: E402
from kitlib.git import repo as gitrepo                              # noqa: E402

USAGE = 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2 (a usage error is never worded as a refusal)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def parser():
    p = Parser(prog='kit_stubs.py', description="Write or check a repository's kit tool stubs.")
    p.add_argument('verb', choices=('write', 'check', 'memory'))
    p.add_argument('-C', dest='root', default='.', help='the checkout (default: here)')
    p.add_argument('--tools', help='comma list; default .kit.json "stubs".tools, else the shared tools')
    p.add_argument('--replace', action='store_true',
                   help='write: replace files that are not stubs (a repository\'s own copies)')
    p.add_argument('--dry-run', action='store_true', help='write: say what would be written, write nothing')
    p.add_argument('--apply', action='store_true',
                   help='memory: write the key (without it memory is a dry run)')
    return p


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    if a.verb == 'check' and (a.replace or a.dry_run):
        p.error('--replace and --dry-run are write options')
    if a.verb == 'memory' and (a.tools is not None or a.replace or a.dry_run):
        p.error('memory takes -C and --apply only (it is a dry run without --apply)')
    if a.verb != 'memory' and a.apply:
        p.error('--apply is a memory option')
    tools = None
    if a.tools is not None:
        tools = [t.strip() for t in a.tools.split(',')]
        if not all(tools):
            p.error('--tools needs comma-separated tool names')
    top, why = gitrepo.toplevel(a.root)
    if top is None:
        p.error('-C: %s' % why)
    from kitlib.host import registry
    try:
        if a.verb == 'memory':
            from kitlib.stubs import memory
            outcome = memory.run(top, apply=a.apply)
        else:
            outcome = stubs.run(top, a.verb, tools=tools, replace=a.replace, dry_run=a.dry_run)
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
