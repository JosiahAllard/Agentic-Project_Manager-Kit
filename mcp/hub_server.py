"""hub_server.py: the Action Hub as a stdio MCP server (docs/hub-mcp.md).

A thin entry point (ARCHITECTURE.md, Layers): it serves kitlib.hubmcp.server over this process's
stdin and stdout; each tool calls the hub server on this machine. The plugin manifest
(.claude-plugin/plugin.json) starts it; it takes no arguments. It boots the kit before importing
kitlib (boot/kitboot.py: the source checkout, or the pinned release, verified), and exits on a kit
that fails that check.

The client starts it with the session's environment, so it reads the session mark once, at start
(kitlib.sessionmark): the owner's tools, once they ship, are refused in a session a launcher marked
(docs/guard.md "Session mark").
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'boot'))
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


if __name__ == "__main__":
    sys.exit(main())
