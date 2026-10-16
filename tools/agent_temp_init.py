"""agent_temp_init.py: set up the agents' temp folder alone, without the rest of a machine setup
(docs/machine-setup.md "Agent temp init").

  agent_temp_init.py                 DRY RUN: print the plan (the folder, AGENT_TEMP, the
                                     agent_temp_sweep config key, the sweep task, the folder's ACL)
  agent_temp_init.py --apply         make it
  options: --kit DIR (default: the checkout this file is in)   --home DIR (default: the user's home)
           --agent-temp DIR (default: the machine config's agent_temp_sweep.dir, then the user
                             environment's AGENT_TEMP, then kit-temp at the root of the kit's drive)
           --no-sweep-task   do not register the agent-temp-sweep scheduled task (by default
                             --apply registers it, after one dry run of the sweep printing what it
                             would remove; with the flag the plan prints the line that registers it)
           --register-sweep-task  the default now; still accepted

The same steps machine_setup.py runs for the folder (kitlib.machinesetup.run_agent_temp), and
nothing else: no links, settings, plugin, shortcut or KIT_MACHINE_LOCK. A thin entry point
(ARCHITECTURE.md, Layers): it parses, calls kitlib.machinesetup, prints and exits. Exit codes: 0
done, or nothing to do; 1 refused or stopped part way (it prints what was done and how to restore);
2 usage.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import machinesetup                                     # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(prog='agent_temp_init.py', description=__doc__.split('\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--kit', default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p.add_argument('--home', default=os.path.expanduser('~'))
    p.add_argument('--agent-temp', default=None)
    p.add_argument('--no-sweep-task', action='store_true',
                   help="do not register the agents' temp sweep task (registered by default)")
    p.add_argument('--register-sweep-task', action='store_true',
                   help='the default; still accepted')
    p.add_argument('--apply', action='store_true')
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return machinesetup.EXIT_USAGE if e.code else 0
    if a.no_sweep_task and a.register_sweep_task:
        print('agent-temp-init: usage - give --no-sweep-task or --register-sweep-task, not both',
              file=sys.stderr)
        return machinesetup.EXIT_USAGE
    safe_stdout()
    ctx = machinesetup.AgentTempContext(a.home, a.kit, agent_temp=a.agent_temp,
                                        sweep_task=not a.no_sweep_task)
    code, _ = machinesetup.run_agent_temp(ctx, apply=a.apply, out=lambda line: print(line, flush=True))
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
