"""Where the bridge's entry points live, relative to the kit root and as paths in this kit.

The service verifies its helpers and the MCP frontend by these exact script paths (a process's
command line must name them), so they are written once here. Entry scripts run under isolated
Python (-I -B) and put the kit root on sys.path themselves before importing this.
"""
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]

FRONTEND_REL = 'optional/bridge/mcp_frontend.py'
SERVICE_REL = 'optional/bridge/tools/bridge_service.py'
RPC_WORKER_REL = 'optional/bridge/tools/bridge_rpc_worker.py'
PIPE_WORKER_REL = 'optional/bridge/tools/bridge_pipe_worker.py'
HOSTS_REL = 'optional/bridge/hosts'

WORKERS = {'bridge_pipe_worker.py': PIPE_WORKER_REL, 'bridge_rpc_worker.py': RPC_WORKER_REL}


def kit_file(rel, kit=None):
    """`rel` (a path above, '/'-separated) under `kit` (default: this kit)."""
    return Path(KIT if kit is None else kit).joinpath(*rel.split('/'))
