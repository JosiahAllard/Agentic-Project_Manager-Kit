"""Read metadata from the running local app-server; never create or message a session.
A thin entry of the Codex shell (hosts/codex/README.md); it boots the kit first through boot/kitboot.py of the export or checkout it sits in.
"""
import argparse
import json
import os
from pathlib import Path
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', '..', '..', 'boot'))  # the kit's own boot/kitboot.py
import kitboot                                                       # noqa: E402
kitboot.boot()                                                       # the kit, checked, before kitlib
import kitlib  # noqa: E402,F401  (after the boot check: installs the CreateProcess hook)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("operation", choices=("list", "loaded", "usage", "models"))
    parser.add_argument("--cwd")
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--codex-exe")
    parser.add_argument("--socket", help="explicit existing local app-server control socket")
    args = parser.parse_args()
    try:
        from hosts.codex.adapter.sessions import ReadOnlyAppServer, SessionHost
        from hosts.codex.adapter.sessions import native_codex
        argv = [str(native_codex(args.codex_exe)), "app-server", "proxy"]
        if args.cwd is not None and not Path(args.cwd).is_absolute():
            raise ValueError('session checkout filter must be absolute')
        if not 1 <= args.max_pages <= 100:
            raise ValueError('pagination bound must be between 1 and 100')
        if args.socket:
            if not Path(args.socket).is_absolute():
                raise ValueError("control socket path must be absolute")
            argv += ["--sock", args.socket]
        with ReadOnlyAppServer(argv=argv) as client:
            host = SessionHost(client.request)
            value = host.inventory(cwd=args.cwd, max_pages=args.max_pages) if args.operation == "list" else getattr(host, args.operation)()
            print(json.dumps(value))
        return 0
    except (ValueError, RuntimeError, OSError, EOFError, TimeoutError) as error:
        sys.stderr.write("Codex session metadata unavailable: %s\n" % error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
