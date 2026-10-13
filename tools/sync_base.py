"""sync_base.py: merge exactly the base commit the coordinator named into your branch (docs/sync-base.md).

  sync_base.py [-C DIR] <hash> --model "<model name>" [--host NAME] [--base BRANCH] [--no-wait]

Merges EXACTLY <hash>, never a rebase. Fetches <remote> <base> first and refuses a hash that is
not on the fetched <remote>/<base> (use the `git ls-remote <remote> <base>` hash, never local
<base>). Conflicts stop with the merge in progress and the trailer already in git's prepared
message; conflict_advice() in kitlib.syncbase carries the per-line audit for doc conflicts. A clean
merge is checked for regrowth (text the base deleted that came back).

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.syncbase, prints and exits.
With `.kit.json` "landing".in_flight set it first waits for a landing in flight (--no-wait: says so
and goes on).
Exit codes: 0 merged and clean, 1 refused / NOT FOUND / conflicts / regrowth / the size reader
died, 2 usage, configuration or the main-checkout refusal, 130 Ctrl+C.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import syncbase                                         # noqa: E402
from kitlib.host import registry                                    # noqa: E402


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(syncbase.USAGE)


def build_parser():
    """allow_abbrev=False: a prefix of a long flag is a usage error, never taken as the flag."""
    p = Parser(prog='sync_base.py', allow_abbrev=False,
               description='Merge exactly the base commit the coordinator named.')
    p.add_argument('-C', dest='root', default='.', metavar='DIR',
                   help='the worktree to merge into (default: here)')
    p.add_argument('hash', help='the base commit to merge, exactly as the coordinator named it')
    p.add_argument('--model', help='the model you are running on (else %s)' % syncbase.MODEL_ENV)
    p.add_argument('--host', '--provider', dest='host', default=None,
                   help='the agent host, for the Co-Authored-By trailer (default: $PM_KIT_HOST, '
                        'else the hosts.json default); --provider is its old name, kept for one release')
    p.add_argument('--base', help='the base branch (default: .kit.json "repo".main_branch)')
    p.add_argument('--no-wait', dest='wait', action='store_false',
                   help='do not wait for a landing in flight (.kit.json "landing".in_flight); '
                        'say so and go on')
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(sys.argv[1:] if argv is None else argv)
    if not a.root or not a.root.strip():
        p.error('-C needs a directory')
    if a.base is not None and (not a.base.strip() or a.base.startswith('-')):
        p.error('--base needs a branch name')
    try:
        host = registry.choose(a.host)
    except registry.RegistryError as error:
        p.error('--host: %s' % error)
    try:
        model, source = syncbase.resolve_model(a.model, provider=host)
    except ValueError as error:
        p.error(str(error))
    if model is None:
        p.error('--model is required: name the model you are running on, --model "<model name>" '
                '(or set %s for this session only); it goes in the merge commit\'s '
                'Co-Authored-By trailer. If you do not know it, say so and stop.'
                % syncbase.MODEL_ENV)
    try:
        out = syncbase.sync(a.root, a.hash, model, source, a.base, wait=a.wait,
                            say=lambda line: print(line, flush=True), provider=host)
    except KeyboardInterrupt:
        return 130
    for line in out.lines:
        print(line, flush=True)
    return out.code


if __name__ == '__main__':
    sys.exit(main())
