"""machine_setup.py: set up this machine for the kit, as one reviewed command (docs/machine-setup.md).
It writes nothing into any repository: a repository works with the kit with nothing committed in it.

  machine_setup.py [--owner-email EMAIL]               DRY RUN of the machine setup: the guard's
                                                       entry in each account's settings.json, the
                                                       kit plugin per account (then, there, the 3
                                                       machine-lock tools' allow rules and the drop
                                                       of the kit SessionStart hooks the plugin
                                                       runs), the owner's email in the machine
                                                       config, the agents' temp folder
  machine_setup.py ... --apply                         make them
  machine_setup.py --old-kit DIR [--apply]             switch the Claude config folders from an
                                                       older kit clone instead (settings, the
                                                       owner's email, the extras)
                                                       In both modes no link is made: each link an
                                                       earlier setup left is reported with the
                                                       command that removes the link only
  machine_setup.py --repo PATH --owner-email EMAIL --agent-email EMAIL [--apply]
                                                       the committed repo setup instead: writes
                                                       .kit.json, the ignore lines, a starter
                                                       docs/INDEX.md (and with --stubs, --stack
                                                       their files) into the repository PATH
                                                       (docs/install.md "Committed setup (--repo)")
  machine_setup.py --managed-settings [--home DIR] [--kit DIR] [--apply]
                                                       write Claude Code's machine-wide managed
                                                       settings file (Program Files/ClaudeCode)
                                                       from the kit's template and the overlay
                                                       ~/.pm-kit/managed-settings.json instead;
                                                       --apply needs an elevated terminal
                                                       (docs/machine-setup.md "Managed settings")
  machine_setup.py --pm-repo PATH [--kit DIR] [--apply]
                                                       set the PM-repo pointer (HKLM\\SOFTWARE\\pm-kit
                                                       pm_repo) to the PM repository's work tree
                                                       PATH instead; --apply needs an elevated
                                                       terminal (docs/machine-setup.md "PM repo
                                                       pointer")
  machine-wide options: --config-dir DIR (repeatable; default: the plugin's account folders, ~/.claude,
                             every ~/.claude-* folder but the machine config's own and each folder
                             of the launcher's accounts and orgs; with --old-kit, the first two)
           --owner-email EMAIL (default: the machine config's; with none, --apply in a console asks,
                             offering `git config user.email`, which is never stored unconfirmed)
           --pm-settings FILE (default mode only: the settings file the PM session loads, for its
                             SessionStart hook)
           --agent-temp DIR (default: the machine config's agent_temp_sweep.dir, then the user
                             environment's AGENT_TEMP, then kit-temp at the root of the kit's drive)
           --no-sweep-task   do not register the agent-temp-sweep scheduled task (by default
                             --apply registers it, after one dry run of the sweep printing what it
                             would remove; a task already registered is left as is)
           --register-sweep-task  the default now; still accepted
           --shortcut        the machine schedule shortcut on the desktop (always with --old-kit)
           --procwatch       register (or update) the "Claude procwatch" scheduled task: every 15 min,
                             report only, its log in the agents' temp folder
           --procwatch-kill  with --procwatch: the task also ends the SAFE rules' processes
           --desktop DIR (default: the user's Desktop)   --home DIR (default: the user's home)
           --kit DIR (default: the checkout this file is in)
  repo options (--repo): --agent-email EMAIL (required) --main-branch NAME (default main)
           --repo-name NAME --stack NAME --stubs --pm-data DIR [--private-remote URL]
  --fresh is the default mode's old name, still accepted. Every mode re-points settings hooks that
  run a retired kit hook script to boot/hook.py.

A thin entry point (ARCHITECTURE.md, Layers): it parses, asks for the owner's email when it must,
calls kitlib.machinesetup, prints and exits. Exit codes: 0 done, or nothing to do; 1 refused or
stopped part way (it prints what was done and how to restore); 2 usage; with --repo --pm-data
--apply, pm_data.py's own non-zero code.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kitlib import machinesetup                                     # noqa: E402
from kitlib.config import user as kuser                             # noqa: E402
from kitlib.machinesetup import owner as mowner                     # noqa: E402
from kitlib.machinesetup import pointer as mpointer                 # noqa: E402
from kitlib.machinesetup import repo as mrepo                       # noqa: E402

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ONLY = ('agent_email', 'main_branch', 'repo_name', 'stack', 'stubs', 'pm_data', 'private_remote')
MACHINE_ONLY = ('config_dir', 'pm_settings', 'agent_temp', 'register_sweep_task', 'no_sweep_task',
                'shortcut', 'procwatch', 'procwatch_kill',
                'desktop', 'home')


def parser():
    p = argparse.ArgumentParser(prog='machine_setup.py', description=__doc__.split('\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--old-kit', help='the older kit clone to switch the config folders from')
    mode.add_argument('--fresh', action='store_true', help='the default mode (its old name)')
    mode.add_argument('--repo', help='the committed repo setup of this repository instead')
    mode.add_argument('--managed-settings', action='store_true',
                      help="write the default host's machine-wide managed settings file from the "
                           "kit's template instead (--apply needs an elevated terminal)")
    mode.add_argument('--pm-repo', help="set the PM-repo pointer in the registry to this PM repository's "
                                        'work tree instead (--apply needs an elevated terminal)')
    p.add_argument('--owner-email', default=None, help="the owner's email (the machine config; with "
                   '--repo, .kit.json "owner".email)')
    p.add_argument('--pm-settings', default=None,
                   help='default mode: the settings file the PM session loads, for its SessionStart hook')
    p.add_argument('--kit', default=KIT)
    p.add_argument('--config-dir', action='append', default=None)
    p.add_argument('--home', default=None)
    p.add_argument('--agent-temp', default=None)
    p.add_argument('--desktop', default=None)
    p.add_argument('--no-sweep-task', action='store_true',
                   help="do not register the agents' temp sweep task (registered by default)")
    p.add_argument('--register-sweep-task', action='store_true',
                   help='the default; still accepted')
    p.add_argument('--shortcut', action='store_true', help='the machine schedule shortcut on the desktop')
    p.add_argument('--procwatch', action='store_true',
                   help='register or update the procwatch scheduled task (report only)')
    p.add_argument('--procwatch-kill', action='store_true',
                   help="with --procwatch: the task also ends the SAFE rules' processes")
    p.add_argument('--agent-email', default=None, help='--repo: the agents\' commit email')
    p.add_argument('--main-branch', default=None, help='--repo: the main branch (default main)')
    p.add_argument('--repo-name', default=None, help="--repo: the repository's short name")
    p.add_argument('--stack', default=None, help='--repo: a stack template (templates/stacks/<name>)')
    p.add_argument('--stubs', action='store_true', help='--repo: write the kit tool stubs')
    p.add_argument('--pm-data', default=None, help="--repo: make the kit's PM data branch here after")
    p.add_argument('--private-remote', default=None, help='--repo, with --pm-data: its private remote')
    p.add_argument('--apply', action='store_true')
    return p


def usage(text):
    sys.stderr.write('machine_setup.py: %s\n' % text)
    return machinesetup.EXIT_USAGE


def main(argv=None):
    try:
        a = parser().parse_args(argv)
    except SystemExit as e:
        return machinesetup.EXIT_USAGE if e.code else 0
    safe_stdout()
    out = lambda line: print(line, flush=True)              # noqa: E731
    if a.managed_settings:
        given = [n for n in REPO_ONLY + MACHINE_ONLY + ('owner_email',)
                 if n != 'home' and getattr(a, n) not in (None, False)]
        if given:
            return usage('--%s does not go with --managed-settings (it takes --kit, --home and --apply)'
                         % given[0].replace('_', '-'))
        home = a.home if a.home is not None else kuser.home_dir()
        code, _ = machinesetup.run_managed(machinesetup.ManagedContext(home, a.kit), apply=a.apply, out=out)
        return code
    if a.pm_repo is not None:
        given = [n for n in REPO_ONLY + MACHINE_ONLY + ('owner_email',) if getattr(a, n) not in (None, False)]
        if given:
            return usage('--%s does not go with --pm-repo (it takes --kit and --apply)'
                         % given[0].replace('_', '-'))
        code, _ = machinesetup.run_pointer(mpointer.PointerContext(a.pm_repo, a.kit), apply=a.apply, out=out)
        return code
    if a.repo is not None:
        given = [n for n in MACHINE_ONLY if getattr(a, n) not in (None, False)]
        if given:
            return usage('--%s goes with the machine setup, not --repo' % given[0].replace('_', '-'))
        ctx = mrepo.RepoContext(a.repo, a.owner_email, a.agent_email, a.kit,
                                main_branch=a.main_branch or 'main', repo_name=a.repo_name or '',
                                stack=a.stack or '', pm_data=a.pm_data or '',
                                private_remote=a.private_remote or '', stubs=a.stubs)
        code, _ = machinesetup.run_repo(ctx, apply=a.apply, out=out)
        return code
    given = [n for n in REPO_ONLY if getattr(a, n) not in (None, False)]
    if given:
        return usage('--%s goes with --repo only' % given[0].replace('_', '-'))
    if a.no_sweep_task and a.register_sweep_task:
        return usage('give --no-sweep-task or --register-sweep-task, not both')
    if a.procwatch_kill and not a.procwatch:
        return usage('--procwatch-kill goes with --procwatch')
    if a.pm_settings and a.old_kit is not None:
        return usage('--pm-settings goes with the default mode only, not --old-kit')
    if a.old_kit is not None and not os.path.isdir(a.old_kit):
        return usage('no old kit folder at %s' % a.old_kit)
    home = a.home if a.home is not None else kuser.home_dir()       # the home fence applies to the default
    ctx = machinesetup.Context(home, a.kit, a.old_kit, config_dirs=a.config_dir,
                               agent_temp=a.agent_temp, desktop=a.desktop,
                               pm_settings=a.pm_settings, sweep_task=not a.no_sweep_task,
                               owner_email=a.owner_email, shortcut=True if a.shortcut else None,
                               procwatch=a.procwatch, procwatch_kill=a.procwatch_kill)
    if a.apply and not ctx.owner_email and interactive() and mowner.needs_prompt(ctx):
        ctx.owner_email = ask_owner_email(mowner.suggestion(ctx))
    code, _ = machinesetup.run(ctx, apply=a.apply, out=out)
    return code


def interactive():
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def ask_owner_email(offered=None):
    """The owner's email typed at the console, or `offered` (git config user.email) when the owner
    answers y to it; None when the answer is empty, no, or not an email (the run then notes that
    none is stored)."""
    try:
        if offered:
            typed = input('Is %s the email your own commits are authored with? Owner-only changes are '
                          'proven by it. y to store it, another address to store that, empty to skip: '
                          % offered).strip()
            if typed.lower() in ('y', 'yes'):
                return offered
            if typed.lower() in ('n', 'no'):
                return None
        else:
            typed = input('The email your own commits are authored with (owner-only changes are proven by '
                          'it; empty to skip): ').strip()
    except EOFError:
        return None
    if typed and mowner.valid(typed):
        print('machine_setup.py: %r is not an email address; none stored' % typed, flush=True)
        return None
    return typed or None


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
