"""plugin_refresh.py: export pushed kit main as the pm-kit plugin's source and update every Claude
account folder's install from it (docs/plugin-source.md).

  plugin_refresh.py                     DRY RUN: print what --apply would do; writes nothing
  plugin_refresh.py --apply             export, point the marketplace at it, prune, then per account
                                        folder: claude plugin marketplace update pm-kit,
                                        claude plugin update pm-kit@pm-kit
  plugin_refresh.py --migrate [--apply] once per machine, from an install of the kit checkout: per
                                        folder marketplace remove, marketplace add <export root>,
                                        install; lists the old caches (never removes them)
  options: --kit DIR (default: the checkout this file is in)   --home DIR (default: the user's home)

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.pluginsource.refresh, prints
and exits. Exit codes: 0 done, or nothing to do; 1 refused, or a folder's claude command failed; 2 usage.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import pluginsource                                      # noqa: E402
from kitlib.config.types import ConfigError                          # noqa: E402
from kitlib.pluginsource import claude                               # noqa: E402

EXIT_USAGE = 2


def main(argv=None):
    p = argparse.ArgumentParser(prog='plugin_refresh.py', description=__doc__.split('\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--kit', default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p.add_argument('--home', default=None)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--migrate', action='store_true')
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else 0
    safe_stdout()

    def out(line):
        print(line, flush=True)

    try:
        s = pluginsource.load(a.home)
    except (ConfigError, pluginsource.RefreshError) as e:
        out('REFUSED: %s' % e)
        return 1
    folders = claude.account_folders(a.home)
    return pluginsource.refresh(os.path.abspath(a.kit), s, folders, out, apply=a.apply,
                                migrate=a.migrate, runner=claude.Runner(a.home))


def safe_stdout():
    """A path the console code page cannot spell (a cp1252 redirect) prints replaced, not raises."""
    try:
        sys.stdout.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
