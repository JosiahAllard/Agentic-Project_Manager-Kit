"""Prepare private Windows bridge installation files for review."""
import argparse
from pathlib import Path
import re
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import kitlib  # noqa: E402,F401  (installs the CreateProcess hook before anything starts)
from optional.bridge.win.install import prepare_file


def _reason(error):
    # An identifier only: never the message, a path or a value.
    code=getattr(error,'code',None)
    if isinstance(code,str) and re.fullmatch(r'[a-z][a-z0-9-]{0,63}',code): return code
    if isinstance(error,FileNotFoundError): return 'input file not found'
    return type(error).__name__


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    try: prepared=prepare_file(args.config,args.output)
    except Exception as error:
        print('Bridge installation preparation failed; no native configuration was activated.',f'Reason: {_reason(error)}',file=sys.stderr)
        return 1
    print('Prepared installation files:',prepared.root)
    print('No native configuration was activated.')
    return 0


if __name__=='__main__': sys.exit(main())
