"""One landing in its own process, for tests/gate/test_land.py's cross-process test:
`python -m tests.gate.land_child <json>` from the kit root. The json names the scratch repo, the
branch and hashes, `wait` (seconds), the scratch lock store (root, kinds path, fake hardware) and
`hold`: with it the merged gate prints INSIDE and waits for a line on stdin before it gates, holding
the landing lock meanwhile. It prints the outcome's lines and exits with its code. It never
touches the machine's lock store, a real repository or a real remote."""
import json
import os
import sys

from kitlib.gate import land, runner
from kitlib.lock import layout as lay
from kitlib.lock.store import Settings, Store
from tests.lock.helpers import FAST


def scratch_store(spec):
    """A Store like tests/lock/helpers.StoreCase.make_store: fake hardware, FAST settings, game
    probes that never look at this machine's processes."""
    s = Store(root=spec['root'], settings=Settings(**FAST),
              hardware=lay.fake_hardware(**spec['hw']), kinds_path=spec['kinds_path'])
    s.game_probes = {'procs': lambda: [], 'services': lambda: [],
                     'pin': lambda pid, mask: 'pinned', 'unpin': lambda pid, g: 'restored',
                     'ctime': lambda pid: 1000 + pid, 'affinity': lambda pid, ct: 0xFFFF}
    return s


def main(argv):
    a = json.loads(argv[1])
    book_kw = {'cores': 1, 'store': scratch_store(a['store']), 'env': dict(os.environ)}
    held = None
    if a.get('hold'):
        def held(lander):
            print('INSIDE', flush=True)
            if not sys.stdin.readline():
                raise SystemExit('land_child: stdin closed before go')
            return runner.merged_gate(lander.root, lander.cfg, lander.expect_main,
                                      log_dir=a['logs'], book_kw=book_kw)
    out = land.Lander(a['repo'], a['branch'], a['expect_main'], a['expect_tip'],
                      wait_s=a.get('wait', 0), git_env=dict(os.environ), book_kw=book_kw,
                      log_dir=a['logs'], merged_gate=held).run()
    for line in out.lines:
        print(line)
    return out.code


if __name__ == '__main__':
    sys.exit(main(sys.argv))
