"""Installed Python-only local broker and optional owner connection window."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import kitlib  # noqa: E402,F401  (installs the CreateProcess hook before anything starts)
from optional.bridge.win.runtime import factories,load_config,run_service


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--connections',action='store_true')
    args=parser.parse_args(argv)
    try:
        config=load_config(args.config)
        providers,deliveries=factories(config)
        run_service(config,providers,delivery_factories=deliveries,connections=args.connections)
    except KeyboardInterrupt: return 0
    except Exception:
        print('Bridge service could not start or remain available.',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__': sys.exit(main())
