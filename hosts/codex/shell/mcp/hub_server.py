"""hub_server.py: the Action Hub as Codex's stdio MCP server (mcp.json; docs/hub-mcp.md). Takes no
arguments. It boots the kit before importing kitlib (boot/kitboot.py of the
export or checkout it sits in, or PM_KIT_ROOT, verified) and exits on a kit that fails
that check. A marked Codex session is refused every hub tool by hosts.codex.adapter.policy.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', '..', '..', 'boot'))  # the kit's own boot/kitboot.py
import kitboot                                                       # noqa: E402
kitboot.boot()                                                       # the kit, checked, before kitlib

from kitlib import sessionmark                                       # noqa: E402
from kitlib.hubmcp.server import Server, serve                      # noqa: E402


def main(environ=None):
    marked = sessionmark.mark(environ)
    rfile, wfile = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr             # a stray print must not corrupt the protocol stream
    serve(Server(marked=marked), rfile, wfile)
    return 0


if __name__ == '__main__':
    sys.exit(main())
