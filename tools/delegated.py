"""delegated.py: run one operation of the owner's delegation list (docs/delegations.md "Running").

  delegated.py run ID [--arg NAME=VALUE]... --trigger Q-nnnn|<ruling id>
      check the trigger, the arguments and the preconditions, log the run in the ledger
      (provenance delegated, not committed), then start the command as an argument list.

--trigger is a hub question the owner answered by choosing the run line `run ID NAME=VALUE ...`
(the operation must list hub-answer and be reversible), or the owner's ledger entry whose Decides
is that line (the operation must list owner-session). Each orders one run.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.delegations.run, prints and
exits. Exit codes: 0 the command ran and exited 0; 1 refused, nothing run and nothing logged;
2 usage error; 4 logged and started (or failed to start), and it failed.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import delegations                                     # noqa: E402
from kitlib.config import user                                     # noqa: E402
from kitlib.config.types import ConfigError                        # noqa: E402
from kitlib.delegations import run as drun                         # noqa: E402

FAILED = 4


class Parser(argparse.ArgumentParser):
    """No abbreviated options, here and in every subparser (parser_class): a permission rule naming
    a flag cannot be stepped around with a prefix of it (docs/allow-rules.md "Abbreviations")."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('allow_abbrev', False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(2)


def parser():
    p = Parser(prog='delegated.py', description="Run one operation of the owner's delegation list.")
    sub = p.add_subparsers(dest='cmd', required=True, parser_class=Parser)
    r = sub.add_parser('run', help='run one delegated operation on the owner\'s order')
    r.add_argument('id', help='the operation id, D-nnn')
    r.add_argument('--arg', action='append', default=[], metavar='NAME=VALUE',
                   help='one declared argument; give each once')
    r.add_argument('--trigger', required=True,
                   help='the hub question Q-nnnn the owner answered, or the owner ledger id')
    return p


def pairs_of(p, args):
    out = []
    for s in args:
        name, sep, value = s.partition('=')
        if not sep or not name:
            p.error('--arg takes NAME=VALUE, not %r' % s)
        out.append((name, value))
    return out


def fail(msg, code=1):
    sys.stderr.write('delegated.py: %s\n' % msg)
    return code


def hub_client():
    from kitlib.hubsite import client, settings
    return client.Client(settings.load().port)


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    pairs = pairs_of(p, a.arg)
    try:
        listed = delegations.load()
    except delegations.DelegationMissing as e:
        return fail('refused: nothing is delegated: %s' % e)
    except delegations.DelegationError as e:
        return fail('refused: %s' % e)
    try:
        cfg = user.user_config()
        hub = hub_client() if drun.Q_ID.fullmatch(a.trigger) else None
    except ConfigError as e:
        return fail('refused: %s' % e)
    try:
        op = drun.find(listed.operations, a.id)
        res = drun.run(op, pairs, a.trigger, cfg, hub=hub)
    except drun.RunRefused as e:
        return fail('refused: %s' % e)
    print('logged %s; ran: %s' % (res.entry, ' '.join(res.argv)))
    if res.error:
        return fail('failed: %s' % res.error, FAILED)
    if res.returncode != 0:
        return fail('failed: exit code %s' % res.returncode, FAILED)
    print('done: exit code 0')
    return 0


if __name__ == '__main__':
    sys.exit(main())
