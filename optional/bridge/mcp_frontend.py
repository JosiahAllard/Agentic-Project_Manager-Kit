"""Python-only MCP launcher for the local bridge's private service."""
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import kitlib  # noqa: E402,F401  (installs the CreateProcess hook before anything starts)
from optional.bridge.win.frontend import serve_frontend


def main():
    if len(sys.argv)>2: return 2
    marker=Path(sys.argv[1]) if len(sys.argv)==2 else Path(os.environ['LOCALAPPDATA'])/'pm-kit/agent-bridge/service.json'
    serve_frontend(sys.stdin.buffer,sys.stdout.buffer,marker)
    return 0


if __name__=='__main__': sys.exit(main())
