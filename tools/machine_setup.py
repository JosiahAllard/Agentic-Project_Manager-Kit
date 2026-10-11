"""machine_setup.py: switch this machine's Claude config folders from an older kit clone to this
kit checkout, or set up a fresh machine that has none, as one reviewed command (docs/machine-setup.md).

  machine_setup.py --old-kit DIR                     DRY RUN: print every change and the settings diffs
  machine_setup.py --old-kit DIR --apply             make them (a .bak beside each settings.json)
  machine_setup.py --fresh [--pm-settings FILE]      DRY RUN of a fresh machine (no older kit clone):
                                                     guard.py's entry, the PM hook in FILE, the kit plugin
                                                     per account
  machine_setup.py --fresh ... --apply               make them
  options: --config-dir DIR (repeatable; default: ~/.claude and every ~/.claude-* folder)
           --kit DIR (default: the checkout this file is in)   --home DIR (default: the user's home)
           --agent-temp DIR (default: the machine config's agent_temp_sweep.dir)
           --desktop DIR (default: the user's Desktop)
  Exactly one of --old-kit and --fresh; --pm-settings goes with --fresh only.

A thin entry point (ARCHITECTURE.md, Layers): it parses, calls kitlib.machinesetup.run, prints and
exits. Exit codes: 0 done, or nothing to do; 1 refused or stopped part way (it prints what was done
and how to restore); 2 usage.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import machinesetup                                     # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(prog='machine_setup.py', description=__doc__.split('\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--old-kit', help='the older kit clone the machine links point into')
    mode.add_argument('--fresh', action='store_true', help='a machine with no older kit clone')
    p.add_argument('--pm-settings', default=None,
                   help='with --fresh: the settings file the PM session loads, for its SessionStart hook')
    p.add_argument('--kit', default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p.add_argument('--config-dir', action='append', default=None)
    p.add_argument('--home', default=os.path.expanduser('~'))
    p.add_argument('--agent-temp', default=None)
    p.add_argument('--desktop', default=None)
    p.add_argument('--apply', action='store_true')
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return machinesetup.EXIT_USAGE if e.code else 0
    if a.pm_settings and not a.fresh:
        sys.stderr.write('machine_setup.py: --pm-settings goes with --fresh only\n')
        return machinesetup.EXIT_USAGE
    if a.old_kit is not None and not os.path.isdir(a.old_kit):
        sys.stderr.write('machine_setup.py: no old kit folder at %s\n' % a.old_kit)
        return machinesetup.EXIT_USAGE
    safe_stdout()
    ctx = machinesetup.Context(a.home, a.kit, a.old_kit, config_dirs=a.config_dir,
                               agent_temp=a.agent_temp, desktop=a.desktop,
                               fresh=a.fresh, pm_settings=a.pm_settings)
    code, _ = machinesetup.run(ctx, apply=a.apply, out=lambda line: print(line, flush=True))
    return code


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
