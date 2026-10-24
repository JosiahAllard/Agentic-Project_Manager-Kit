"""Tests only: the lock MCP server over stdio on a scratch store, never the machine's.

  python scratch_server.py <store root> <kinds.json> owner|agent

The store is made as tests/lock/helpers.StoreCase makes it: its own folder (so its own mutex
names), fake hardware (main's fake_hardware call, two L3 domains), the FAST settings, and game probes that never
look at this machine's processes. A scratch store takes the actor as given (kitlib.lock.api.who),
so the second argument picks the owner or an agent.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from kitlib.lock import layout as lay                              # noqa: E402
from kitlib.lock.store import Settings, Store                      # noqa: E402
from kitlib.lockmcp.server import Server, serve                    # noqa: E402
from tests.lock.helpers import FAST, OWNER, agent                  # noqa: E402


def main(root, kinds_path, who):
    store = Store(root=root, settings=Settings(**FAST), hardware=lay.fake_hardware(
        threads=24, smt=2, l3=2), kinds_path=kinds_path)
    if not store.testing:
        sys.stderr.write("refusing: %s is the machine's store\n" % root)
        return 2
    store.game_probes = {"procs": lambda: [], "services": lambda: [],
                         "pin": lambda pid, mask: "pinned", "unpin": lambda pid, g: "restored",
                         "ctime": lambda pid: 1000 + pid, "affinity": lambda pid, ct: 0xFFFF}
    actor = OWNER if who == "owner" else agent("mcp-test")
    rfile, wfile = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr
    serve(Server(store, actor), rfile, wfile)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
