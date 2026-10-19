"""plugin_refresh.py: export pushed kit main as the pm-kit plugin's source and update every Claude
account folder's install from it (docs/plugin-source.md).

  plugin_refresh.py                     DRY RUN: print what --apply would do; writes nothing
  plugin_refresh.py --apply             export, point the marketplace at it, prune (keeping every
                                        export a running process, scheduled task or pin lease
                                        names), then per account folder: claude plugin marketplace
                                        update pm-kit, claude plugin update pm-kit@pm-kit, in order
  plugin_refresh.py --migrate [--apply] once per machine, from an install of the kit checkout: per
                                        folder marketplace remove, marketplace add <export root>,
                                        install; lists the old caches (never removes them).
                                        Without --migrate, a folder not migrated yet is refused
  plugin_refresh.py --prune-caches [--apply]
                                        each account folder's plugins\\cache\\pm-kit\\pm-kit: list
                                        (dry run) or remove the old versions; never the installed
                                        one, the newest, one replaced in the last day or one a
                                        running process or scheduled task names
  options: --folder DIR (repeatable: only the account folders named; any other DIR is refused)
           --kit DIR (default: the checkout this file is in)   --home DIR (default: the user's home)

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
    p.add_argument('--prune-caches', action='store_true')
    p.add_argument('--folder', action='append', default=[])
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else 0
    if a.prune_caches and a.migrate:
        print('usage: --prune-caches and --migrate cannot be run together', file=sys.stderr)
        return EXIT_USAGE
    safe_stdout()

    def out(line):
        print(line, flush=True)

    try:
        s = pluginsource.load(a.home, kit=os.path.abspath(a.kit))
    except (ConfigError, pluginsource.RefreshError) as e:
        out('REFUSED: %s' % e)
        return 1
    folders, bad = pluginsource.pick_folders(claude.account_folders(a.home), a.folder)
    if bad:
        out('REFUSED: not an account folder on this machine: %s' % ', '.join(bad))
        return 1

    def in_use():
        return pluginsource.read_in_use(s.export_dir)
    if a.prune_caches:
        return prune_caches(s, folders, in_use, a.apply, out)
    return pluginsource.refresh(os.path.abspath(a.kit), s, folders, out, apply=a.apply,
                                migrate=a.migrate, runner=claude.Runner(a.home), in_use=in_use)


def prune_caches(s, folders, in_use, apply, out):
    """--prune-caches: the rows of pluginsource.prune_caches, then the counts. With --apply it
    holds the refresh lock, so no refresh installs while it removes."""
    if not apply:
        out('DRY RUN (nothing removed; --apply removes the versions marked "would remove"):')
        return _cache_rows(pluginsource.prune_caches(folders, in_use), out)
    try:
        os.makedirs(s.export_dir, exist_ok=True)
    except OSError as e:
        out('REFUSED: cannot make the export root %s: %s' % (s.export_dir, e))
        return 1
    lock = pluginsource.RefreshLock(s.export_dir)
    try:
        lock.acquire()
    except pluginsource.Busy as e:
        out('REFUSED: %s' % e)
        return 1
    try:
        return _cache_rows(pluginsource.prune_caches(folders, in_use, apply=True), out)
    finally:
        lock.release()


def _cache_rows(rows, out):
    words = {'kept': 'kept', 'would': 'would remove', 'removed': 'removed', 'failed': 'FAILED to remove'}
    counts = {}
    for path, action, why in rows:
        counts[action] = counts.get(action, 0) + 1
        out('%s %s%s' % (words[action], path, ' (%s)' % why if why else ''))
    out('caches: %s' % (', '.join('%d %s' % (counts.get(k, 0), words[k]) for k in words
                                  if counts.get(k)) or 'no old version found'))
    return 1 if counts.get('failed') else 0


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
