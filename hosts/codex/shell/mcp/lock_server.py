"""lock_server.py: the machine lock as Codex's stdio MCP server, read tools only (mcp.json;
hosts.codex.adapter.lock; docs/lock-mcp.md). Takes no arguments. It boots the kit before importing
kitlib (boot/kitboot.py of the export or checkout it sits in, or PM_KIT_ROOT, verified) and exits on a kit that fails
that check.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', '..', '..', 'boot'))  # the kit's own boot/kitboot.py
import kitboot                                                       # noqa: E402
kitboot.boot()                                                       # the kit, checked, before kitlib

from kitlib.lockmcp.server import serve                             # noqa: E402
from hosts.codex.adapter.lock import ReadOnlyServer                 # noqa: E402


def main():
    rfile, wfile = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr             # a stray print must not corrupt the protocol stream
    serve(ReadOnlyServer(), rfile, wfile)
    return 0


if __name__ == '__main__':
    sys.exit(main())
