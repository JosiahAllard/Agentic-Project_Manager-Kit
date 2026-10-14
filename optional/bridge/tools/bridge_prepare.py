"""Prepare private Windows bridge installation files for review."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from optional.bridge.win.install import prepare_file


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    try: prepared=prepare_file(args.config,args.output)
    except Exception:
        print('Bridge installation preparation failed; no native configuration was activated.',file=sys.stderr)
        return 1
    print('Prepared installation files:',prepared.root)
    print('No native configuration was activated.')
    return 0


if __name__=='__main__': sys.exit(main())
