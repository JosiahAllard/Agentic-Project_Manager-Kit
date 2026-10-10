"""lock_release_check.py: may the kit release take this commit now? (docs/lock-release.md)

  lock_release_check.py [-C KIT] <commit>

Refuses while the release would change the lock tool and any run in the machine's lock store is
live. -C names the release checkout (default: the checkout this file sits in). A thin entry point
(ARCHITECTURE.md, Layers): it parses, calls kitlib.lock.release.check, prints and exits. It reads
the store and the repository and writes neither. Exit codes: 0 OK (prints the git command that
makes the release); 1 STOP; 2 usage; 3 ERROR (an unexpected failure: nothing was checked, so do
not release); 130 Ctrl+C.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib.lock import release                                    # noqa: E402
from kitlib.lock.store import Store                                # noqa: E402

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Parser(argparse.ArgumentParser):
    """argparse whose errors print `USAGE:` and exit 2 (a usage error is never worded as a refusal)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write('USAGE: %s\n' % message)
        sys.exit(release.USAGE)


def parser():
    p = Parser(prog='lock_release_check.py',
               description='May the kit release take this commit while runs are live?')
    p.add_argument('commit', help='the commit (or branch) the release checkout would move to')
    p.add_argument('-C', dest='kit', default=KIT,
                   help='the release checkout (default: the checkout this tool sits in)')
    p.add_argument('--test-root', default=None, help=argparse.SUPPRESS)
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        store = Store(root=a.test_root) if a.test_root else None
        outcome = release.check(a.kit, a.commit, store)
    except KeyboardInterrupt:
        return 130
    except Exception as err:                                       # noqa: BLE001 - ERROR, never OK
        outcome = release.error_outcome(err)
    for line in outcome.lines:
        print(line, flush=True)
    return outcome.code


if __name__ == '__main__':
    sys.exit(main())
