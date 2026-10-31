"""A gate run hung in its own process, for tests/gate/test_gate_stop.py's forced stop:
`python -m tests.gate.stop_child <json>` from the kit root. The json is the scratch lock store (root,
kinds path, fake hardware). It books a gate run, starts one long-lived process in the run's job,
prints {id, child} as a JSON line and then sleeps: it never polls for a stop, as a wrapper stuck
in a check of its own would not. It never touches the machine's lock store."""
import json
import os
import subprocess
import sys
import time

from kitlib.lock import run as lockrun
from tests.gate.land_child import scratch_store

SLEEP = [sys.executable, '-c', 'import time; time.sleep(600)']


def main(argv):
    spec = json.loads(argv[1])
    slot = lockrun.book('test', 'kit', 'hung', cores=1, store=scratch_store(spec),
                        env=dict(os.environ), gate=True, no_wait=True)
    with slot:
        p = slot.launcher.start(SLEEP, cwd=os.getcwd(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=dict(os.environ))
        print(json.dumps({'id': slot.id, 'child': p.pid}), flush=True)
        time.sleep(600)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
