"""node.py: the load nodes in <PM repo>/.pm-kit/load-nodes.json (docs/load-nodes.md).

  node.py list
      each node in the registry: where it is, what it may take, its pinned host key's fingerprint
      and whether its identity file is there; nothing goes on the network
  node.py status [NAME ...] [--json] [--timeout S]
      ask each node (or the ones named) for its machine lock and hardware over ssh, the host key
      pinned; --json prints the checked replies as one JSON object

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.loadnode, prints and exits.
Exit codes: 0 every node asked answered; 1 at least one did not (every node is still listed);
2 usage, or a registry that is missing, invalid or not ignored by git.
"""
import argparse
import json
import os
import sys

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT_ROOT)

from kitlib.loadnode import registry, remote, reply              # noqa: E402

OK, FAILED, USAGE = 0, 1, 2


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(USAGE)


def _timeout(text):
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError('must be a whole number of seconds')
    if not 1 <= v <= 300:
        raise argparse.ArgumentTypeError('must be from 1 to 300 seconds')
    return v


def parser():
    p = Parser(prog='node.py', allow_abbrev=False, description='The load nodes.')
    sub = p.add_subparsers(dest='verb', metavar='{list,status}', parser_class=Parser)
    sub.required = True
    sub.add_parser('list', help='the registry, no network', allow_abbrev=False)
    s = sub.add_parser('status', help='ask each node over ssh', allow_abbrev=False)
    s.add_argument('names', nargs='*', metavar='NAME', help='only these nodes')
    s.add_argument('--json', action='store_true', help='print the checked replies as JSON')
    s.add_argument('--timeout', type=_timeout, default=remote.TIMEOUT_S,
                   help='seconds to wait for each node (default %d)' % remote.TIMEOUT_S)
    return p


def _err(text):
    sys.stderr.write(text + '\n')


def main(argv=None, runner=None):
    a = parser().parse_args(argv)
    try:
        nodes = registry.load(registry.path())
    except registry.RegistryError as e:
        _err('REFUSED: %s' % e)
        return USAGE
    if a.verb == 'list':
        if not nodes:
            print('no load nodes in the registry')
        for line in registry.list_lines(nodes, resolve=remote.identity_path):
            print(line)
        return OK
    known = {n.name for n in nodes}
    unknown = [x for x in a.names if x not in known]
    if unknown:
        _err('USAGE: no node named %s in the registry' % ', '.join(sorted(set(unknown))))
        return USAGE
    chosen = [n for n in nodes if not a.names or n.name in a.names]
    results = remote.query_all(chosen, runner=runner or remote.default_runner, timeout_s=a.timeout)
    if a.json:
        print(json.dumps({r.name: {'state': r.state, 'detail': r.detail, 'reply': r.reply}
                          for r in results}, indent=1, sort_keys=True))
    else:
        if not chosen:
            print('no load nodes in the registry')
        for line in reply.status_lines(results, nodes, reply.kit_version(KIT_ROOT)):
            print(line)
    return OK if all(r.state == remote.OK for r in results) else FAILED


if __name__ == '__main__':
    sys.exit(main())
