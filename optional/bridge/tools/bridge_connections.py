"""Open the running bridge's human chat-connection window."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from optional.bridge.win.owner_view import open_connections


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--marker',type=Path)
    args=parser.parse_args(argv)
    try: open_connections(args.marker)
    except Exception:
        print('The bridge connection window is unavailable.',file=sys.stderr)
        return 1
    print('Requested the chat connection window.')
    return 0


if __name__=='__main__': sys.exit(main())
