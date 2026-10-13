"""lock_server.py: the machine lock as a stdio MCP server (docs/lock-mcp.md).

A thin entry point (ARCHITECTURE.md, Layers): it serves kitlib.lockmcp.server over this
process's stdin and stdout on the machine's lock store. The plugin manifest
(.claude-plugin/plugin.json) starts it; it takes no arguments. It boots the kit before importing
kitlib (boot/kitboot.py: the source checkout, or the pinned release, verified), and exits on a kit
that fails that check.

The client starts it with the session's environment, so it reads the session mark once, at
start (kitlib.sessionmark): in a session nothing marks, a caller under the session's agent host
is the owner (docs/guard.md "Session mark").
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'boot'))
import kitboot                                                       # noqa: E402
kitboot.boot()                                                       # the kit, checked, before kitlib

from kitlib import sessionmark                                       # noqa: E402
from kitlib.lock import api                                          # noqa: E402
from kitlib.lockmcp.server import Server, serve                    # noqa: E402


def main(environ=None):
    api.set_owner_check(sessionmark.owner_check(environ))
    rfile, wfile = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr             # a stray print must not corrupt the protocol stream
    serve(Server(), rfile, wfile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
