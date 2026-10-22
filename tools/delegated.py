"""delegated.py: run one operation of the owner's delegation list (docs/delegations.md "Running").

  delegated.py run ID [--arg NAME=VALUE]... (--trigger Q-nnnn|<ruling id> | --approval hub:A-nnn)
      check the trigger, the arguments and the preconditions, log the run in the ledger
      (provenance delegated, not committed), then start the command as an argument list.
  delegated.py request ID [--arg NAME=VALUE]... --title T [--reason R]
      check the arguments, the program and where the run would act, then ask the owner on the
      hub to approve that one run; prints the new approval's id.

--trigger is a hub question the owner answered by choosing the run line `run ID NAME=VALUE ...`
(the operation must list hub-answer and be reversible), or the owner's ledger entry whose Decides
is that line (the operation must list owner-session). --approval is the hub approval the owner
gave to a request (the operation must list hub-approval; docs/delegations.md "Hub approvals").
Each orders one run.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.delegations.run, prints and
exits. Exit codes: 0 the command ran and exited 0, or the request was added; 1 refused, nothing
run, logged or asked; 2 usage error; 4 logged and started (or failed to start), and it failed.

Run it as `python -I`. Without -I (or -E) it refuses when any PYTHON* variable is set, before it
imports anything but os and sys: PYTHONPATH can shadow argparse or subprocess before any check runs.
"""
import os
import sys


def python_vars(environ, flags):
    """The PYTHON* names of `environ` that steer this interpreter: none when `flags` (sys.flags)
    say it ignores them (-I or -E)."""
    if flags.ignore_environment:
        return []
    return sorted(k for k in environ if k.upper().startswith('PYTHON'))


if __name__ == '__main__':
    _steering = python_vars(os.environ, sys.flags)
    if _steering:
        sys.stderr.write('delegated.py: refused: %s set, and a PYTHON* variable steers this '
                         'interpreter before any check runs: run it as python -I '
                         '(docs/delegations.md "Running")\n' % ', '.join(_steering))
        sys.exit(1)

import argparse                                                    # noqa: E402

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
    on = r.add_mutually_exclusive_group(required=True)
    on.add_argument('--trigger',
                    help='the hub question Q-nnnn the owner answered, or the owner ledger id')
    on.add_argument('--approval', metavar='hub:A-nnn',
                    help='the hub approval the owner gave for this run')
    q = sub.add_parser('request', help='ask the owner on the hub to approve one run')
    q.add_argument('id', help='the operation id, D-nnn')
    q.add_argument('--arg', action='append', default=[], metavar='NAME=VALUE',
                   help='one declared argument; give each once')
    q.add_argument('--title', required=True, help='the approval card\'s title')
    q.add_argument('--reason', help='why the run is wanted, for the owner')
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


def request(a, pairs, listed):
    try:
        cfg = user.user_config()
        hub = hub_client()
    except ConfigError as e:
        return fail('refused: %s' % e)
    try:
        op = drun.find(listed.operations, a.id)
        rid = drun.request(op, pairs, cfg, hub, a.title, reason=a.reason)
    except drun.RunRefused as e:
        return fail('refused: %s' % e)
    print('requested hub:%s: the owner approves or denies it on the hub; then run it with '
          'delegated.py run %s --approval hub:%s' % (rid, op.id, rid))
    return 0


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
    if a.cmd == 'request':
        return request(a, pairs, listed)
    approval = getattr(a, 'approval', None)
    if approval is not None and not drun.APPROVAL_RE.match(approval):
        return fail('refused: --approval takes hub:A-nnn, not %r' % approval)
    trigger = approval if approval is not None else a.trigger
    try:
        cfg = user.user_config()
        hub = hub_client() if approval is not None or drun.Q_ID.fullmatch(trigger) else None
    except ConfigError as e:
        return fail('refused: %s' % e)
    try:
        op = drun.find(listed.operations, a.id)
        res = drun.run(op, pairs, trigger, cfg, hub=hub)
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
