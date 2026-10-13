"""pending.py: the owner's pending-steps file named by the machine config's `pending_path`
(docs/pending.md).

  pending.py list                      the open items, `N. text` (first line each).
  pending.py add --text T [COMMIT]     append an item numbered one past the highest; prints `added N`.
  pending.py done N [--note T] [--when MM-DD] [COMMIT]
                                       mark item N `DONE MM-DD (note). Was: ...`; prints `done N`.
  pending.py edit N --text T [COMMIT]  replace item N and its continuation lines; prints `edited N`.

  COMMIT: [--commit | --push] [--branch-ok] [--model NAME] [--host NAME]
      --commit commits the file alone as `pending: <added|done|edited> N`; --push also pushes the
      current branch to the repository's `repo.remote` (origin by default). Both refuse, before
      writing, when the file has uncommitted changes or the branch is not its repository's main
      branch (unless --branch-ok). The trailer names --model, else $CLAUDE_AGENT_MODEL.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.pending, prints and exits.
Exit codes: 0 ok; 1 no file configured or unreadable, a refused change, a file that cannot be
written, or a commit or push that failed after the file was written (printed as written but NOT
committed, or committed but NOT pushed); 2 usage error.
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import pending                                         # noqa: E402
from kitlib.config import user                                     # noqa: E402
from kitlib.config.types import ConfigError                        # noqa: E402
from kitlib.host import registry                                    # noqa: E402
from kitlib.ledger import vcs                                      # noqa: E402
from kitlib.ledger.errors import Refused, Usage                    # noqa: E402

NOUN = 'the pending file'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(2)


def parser():
    p = Parser(prog='pending.py', description="The owner's pending-steps file.")
    sub = p.add_subparsers(dest='cmd', required=True, parser_class=Parser)

    def commit_flags(sp):
        sp.add_argument('--commit', action='store_true', help='commit only the pending file after the change')
        sp.add_argument('--push', action='store_true', help='implies --commit, then push the branch to repo.remote (origin by default)')
        sp.add_argument('--branch-ok', action='store_true', help='allow --commit off the main branch')
        sp.add_argument('--model', help='the model named in the Co-Authored-By trailer (default: $%s)'
                        % vcs.MODEL_ENV)
        sp.add_argument('--host', '--provider', dest='host', default=None,
                        help='the agent host, for the Co-Authored-By trailer (default: $PM_KIT_HOST, '
                             'else the hosts.json default); --provider is its old name, kept for one release')
    sub.add_parser('list', help='the open items')
    ad = sub.add_parser('add', help='append one item')
    ad.add_argument('--text', required=True)
    commit_flags(ad)
    dn = sub.add_parser('done', help='mark an item done')
    dn.add_argument('n', type=int)
    dn.add_argument('--note')
    dn.add_argument('--when', help='MM-DD (default: today)')
    commit_flags(dn)
    ed = sub.add_parser('edit', help='replace an item')
    ed.add_argument('n', type=int)
    ed.add_argument('--text', required=True)
    commit_flags(ed)
    return p


def fail(msg, code=1):
    sys.stderr.write('pending.py: %s\n' % msg)
    return code


def host_of(a):
    """--host, else PM_KIT_HOST, else the hosts.json default; a bad name is a usage error."""
    try:
        return registry.choose(a.host)
    except registry.RegistryError as e:
        raise Usage('--host: %s' % e) from None


def run(a, path):
    if a.cmd == 'list':
        for line in pending.open_items(pending.read_raw(path)):
            print(line)
        return 0
    plan = None
    if a.commit or a.push:
        plan = vcs.prepare(path, branch_ok=a.branch_ok, model=a.model, noun=NOUN,
                           provider=host_of(a))
    raw = pending.read_raw(path)
    if a.cmd == 'add':
        new, n = pending.add(raw, a.text)
        label = 'added %d' % n
    elif a.cmd == 'done':
        new = pending.done(raw, a.n, a.when or datetime.date.today().strftime('%m-%d'), a.note)
        label = 'done %d' % a.n
    else:
        new = pending.edit(raw, a.n, a.text)
        label = 'edited %d' % a.n
    try:
        pending.write_atomic(path, new)
    except OSError as e:
        return fail('refused: cannot write %s: %s' % (path, e.strerror or e))
    print(label)
    sys.stdout.flush()
    if plan is not None:
        try:
            lines = vcs.commit_one(plan, 'pending: ' + label, NOUN, push=a.push)
        except Refused as e:                    # the file is written: not a refusal
            return fail(str(e))
        for line in lines:
            print(line)
    return 0


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        path = user.user_config()['pending_path']
    except ConfigError as e:
        return fail(str(e))
    if not path:
        return fail('no pending_path in %s' % user.display())
    try:
        return run(a, path)
    except Usage as e:
        parser().print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % e)
        return 2
    except Refused as e:
        return fail('refused: %s' % e)


if __name__ == '__main__':
    sys.exit(main())
