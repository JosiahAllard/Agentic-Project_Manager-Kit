"""Private Windows RPC worker entry point; not an agent-facing bridge command."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from optional.bridge.win.rpcworker import exchange


def main():
    exchange(sys.stdin.buffer,sys.stdout.buffer)
    return 0


if __name__=='__main__': sys.exit(main())
