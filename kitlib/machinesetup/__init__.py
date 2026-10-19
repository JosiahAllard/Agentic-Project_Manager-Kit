"""kitlib.machinesetup: set up a machine for the kit (tools/machine_setup.py,
installer/machine-setup.ps1; docs/machine-setup.md). Plan decision 27: it writes nothing into any
repository; a repository works with the kit with nothing committed in it, and the kit's first use
there writes only local git settings (kitlib.git.prep).

survey() reads the machine and returns a Plan: the changes to make, in order, each with the text a
dry run prints and the function --apply runs, plus the refusals that stop both. Two modes:

- The default (no older kit clone; `fresh`, the old --fresh, is the same): each Claude account's
  settings.json gains the guard's entry (boot/hook.py <host> PreToolUse, until the plugin carries
  the guard), with any hook path to a retired kit hook script re-pointed (settings.repoint_moved);
  the kit plugin is installed on each registered host that machine setup can install
  (kitlib.machinesetup.plugin; every other host is a note naming its README); the owner's email is
  stored in the machine config (kitlib.machinesetup.owner); the agents' temp folder is made with
  its AGENT_TEMP variable, sweep config key and permissions (kitlib.machinesetup.agenttemp,
  extras), and the sweep task (opt-out: `sweep_task` false). Opt-in: the PM SessionStart hook
  (`pm_settings`), the machine schedule shortcut (`shortcut`), the procwatch task (`procwatch`).
- The switch from an older kit clone (`old_kit`): each config folder's settings.json (its old kit
  hooks removed or re-pointed, the guard's entry added), the owner's email, and the extras: the
  shortcut, the agents' temp folder and KIT_MACHINE_LOCK for comfy. No plugin install
  (tools/plugin_refresh.py --migrate does it).

In both modes machine setup makes no link: kitlib.machinesetup.links reports each link an earlier
setup left under a config folder, with the command that removes the link only, and the settings
step re-points a path through `<config>/tools/<link>` to the kit's own file
(settings.repoint_links).

Claude Code's machine-wide managed settings file (kitlib.machinesetup.managed) is read in both
modes and written in neither: when it carries this kit's guard entry, no account settings.json gains
its own, and the default mode notes whether the file is in sync with the
kit's template. Writing it is `--managed-settings` (run_managed), a mode of its own that needs an
elevated terminal.

The committed repo setup is kitlib.machinesetup.repo (`--repo PATH`, run_repo), a mode of its own.
run_agent_temp() does the agents' temp folder steps alone (tools/agent_temp_init.py).
Nothing here prints or exits; run() gives the exit code and the lines.
"""
import datetime
import os

from kitlib.config import user
from kitlib.config.migration import OLD_DIR_NAME

from . import agenttemp, extras, links, managed, owner, plugin, settings
from .common import Change, Plan, Report, kit_file

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2


class Context(object):
    """What a survey reads. `host` is the Windows side (extras.Host): shortcuts, icacls, the user
    environment, the guard's settings entry, `git config user.email` and the `claude` runner; tests
    pass a fake. No `old_kit` is the default mode (`fresh` says the same and may not go with
    `old_kit`); `pm_settings` goes with the default mode only. `owner_email` is --owner-email.
    `shortcut` None: the machine schedule shortcut in the old kit switch only. `procwatch`: also
    register the procwatch task (kitlib.machinesetup.procwatch); `procwatch_kill` makes it kill."""

    def __init__(self, home, kit, old_kit=None, config_dirs=None, agent_temp=None, desktop=None,
                 host=None, today=None, fresh=False, pm_settings=None, sweep_task=True,
                 owner_email=None, shortcut=None, procwatch=False, procwatch_kill=False):
        if fresh and old_kit:
            raise ValueError('give old_kit or fresh, not both')
        fresh = not old_kit
        if pm_settings and not fresh:
            raise ValueError('pm_settings goes with the default mode only, not old_kit')
        self.home = os.path.abspath(home)
        self.kit = os.path.abspath(kit)
        self.fresh = fresh
        self.owner_email = (owner_email or '').strip() or None
        self.shortcut = (not fresh) if shortcut is None else bool(shortcut)
        self.old_kit = None if fresh else os.path.abspath(old_kit)
        self.pm_settings = os.path.abspath(pm_settings) if pm_settings else None
        self.config_dirs_given = bool(config_dirs)
        if not config_dirs:
            config_dirs = account_dirs(self.home) if fresh else default_config_dirs(self.home)
        self.config_dirs = dedupe(os.path.abspath(d) for d in config_dirs)
        self.agent_temp = agent_temp
        self.sweep_task = bool(sweep_task)
        if procwatch_kill and not procwatch:
            raise ValueError('procwatch_kill goes with procwatch')
        self.procwatch = bool(procwatch)
        self.procwatch_kill = bool(procwatch_kill)
        self.desktop = desktop
        self.host = host or extras.Host()
        self.today = today or datetime.date.today().strftime('%Y%m%d')
        self.managed_file = None                                     # managed.survey_live sets both
        self.managed_data = None


def default_config_dirs(home):
    """`<home>/.claude` and every `<home>/.claude-*` folder, the kit's own machine config folders
    excepted: the current one and the retired one (kitlib.config.migration.OLD_DIR_NAME), which
    starts `.claude-` and is no Claude account folder."""
    out = []
    try:
        names = sorted(os.listdir(home))
    except OSError:
        return out
    for n in names:
        if (n == '.claude' or n.startswith('.claude-')) and n not in (user.DIR_NAME, OLD_DIR_NAME) \
                and os.path.isdir(os.path.join(home, n)):
            out.append(os.path.join(home, n))
    return out


def account_dirs(home):
    """The account folders a fresh run edits when none is named: kitlib.pluginsource.claude
    account_folders, the set tools/plugin_refresh.py runs in (every folder default_config_dirs
    lists, then the launcher's account and org folders that exist)."""
    from kitlib.pluginsource import claude as pclaude
    return pclaude.account_folders(home)


def dedupe(dirs):
    """`dirs` in order, each real folder once: a folder that is a link to another one listed (an
    account folder following ~/.claude) would otherwise be edited twice."""
    out, seen = [], set()
    for d in dirs:
        key = os.path.normcase(os.path.realpath(d))
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def survey(ctx):
    plan = Plan(Plan.FRESH_SECTIONS if ctx.fresh else None)
    if not os.path.isfile(kit_file(ctx.kit, settings.GUARD)):
        plan.refuse('%s is not a kit checkout (no %s)' % (ctx.kit, settings.GUARD))
        return plan
    if not ctx.fresh and os.path.normcase(ctx.kit) == os.path.normcase(ctx.old_kit):
        plan.refuse('the old kit and the kit are the same folder: %s' % ctx.kit)
        return plan
    if not ctx.config_dirs:
        plan.refuse('no Claude account folder under %s (no .claude or .claude-* folder): start Claude '
                    'Code once so it makes ~/.claude, or name a folder with --config-dir' % ctx.home)
        return plan
    for d in ctx.config_dirs:
        if not os.path.isdir(d):
            plan.refuse('no config folder at %s' % d)
    if plan.refusals:
        return plan
    if ctx.fresh and not ctx.config_dirs_given:
        plan.note('links', 'config folders: every ~/.claude and ~/.claude-* folder (a backup copy '
                           'too) and the launcher\'s account folders, the set tools/plugin_refresh.py '
                           'runs in; name the ones to edit with --config-dir to leave any other out')
    for d in ctx.config_dirs:
        links.survey(plan, d, ctx)                                   # notes only: no link is made
    managed.survey_live(plan, ctx)                                   # the guard, the plugin: from there?
    if plan.refusals:
        return plan
    for d in ctx.config_dirs:
        settings.survey(plan, d, ctx)
    managed.note_sync(plan, ctx)                                     # never written in this mode
    if ctx.fresh:
        settings.survey_pm(plan, ctx)
        plugin.survey_hosts(plan, ctx)
    owner.survey(plan, ctx)
    extras.survey(plan, ctx)
    return plan


def run(ctx, apply=False, out=None):
    """(exit code, lines): the plan, and with `apply` its changes made in order. `out`, when given,
    is called with each line as it is produced, so a caller that prints through it has every line
    up to a failure even if the process dies there."""
    lines = []

    def emit(*ls):
        for l in ls:
            lines.append(l)
            if out:
                out(l)

    emit('machine-setup: %s' % ('APPLY' if apply else 'DRY RUN (nothing changed; add --apply to act)'),
         '  kit:            %s' % ctx.kit,
         '  old kit:        %s' % (ctx.old_kit or 'none (the default mode)'),
         '  config folders: %s' % ', '.join(ctx.config_dirs))
    if ctx.fresh:
        emit('  pm settings:    %s' % (ctx.pm_settings or 'none (no --pm-settings)'))
    return _carry_out('machine-setup', survey(ctx), apply, emit, lines)


def _carry_out(name, plan, apply, emit, lines):
    """Print `plan` and, with `apply`, make its changes in order: (exit code, lines)."""
    emit(*plan.lines())
    if plan.refusals:
        emit(*['%s: REFUSED - %s' % (name, r) for r in plan.refusals])
        emit('%s: nothing changed' % name)
        return EXIT_REFUSED, lines
    if not plan.changes:
        emit('%s: nothing to do' % name)
        return EXIT_OK, lines
    if not apply:
        emit('%s: %d change(s) to make; re-run with --apply to make them' % (name, len(plan.changes)))
        return EXIT_OK, lines
    done = []
    for c in plan.changes:
        try:
            said = c.do()
        except BaseException as e:                                   # any failure gets the report
            emit('%s: STOPPED at: %s' % (name, c.text),
                 '  error: %s: %s' % (type(e).__name__, e))
            if c.undo:
                emit('  restore: %s' % c.undo)
            emit('  already done by this run (%d): %s' % (len(done), '; '.join(done) or 'none'))
            if not isinstance(e, Exception):                         # Ctrl+C, exit: report, then go on
                raise
            return EXIT_REFUSED, lines
        done.append(c.text)
        if isinstance(said, Report):                                 # a change's own report lines
            emit(*['  ' + l for l in said])
        emit('done: %s' % c.text)
    emit('%s: made %d change(s); a second run reports nothing to do' % (name, len(done)))
    return EXIT_OK, lines


def run_repo(ctx, apply=False, out=None):
    """(exit code, lines) of the committed repo setup (kitlib.machinesetup.repo.run; ctx a
    repo.RepoContext): --repo PATH, and installer/new-repo.ps1."""
    from . import repo
    return repo.run(ctx, apply=apply, out=out)


ManagedContext = managed.ManagedContext


def run_managed(ctx, apply=False, out=None):
    """(exit code, lines) of the --managed-settings mode (kitlib.machinesetup.managed.survey; ctx a
    ManagedContext): the managed settings file made from the kit's template, as run()."""
    lines = []

    def emit(*ls):
        for l in ls:
            lines.append(l)
            if out:
                out(l)

    emit('managed-settings: %s' % ('APPLY' if apply else 'DRY RUN (nothing changed; add --apply to act)'),
         '  kit:            %s' % ctx.kit,
         '  template:       %s' % kit_file(ctx.kit, managed.SOURCE),
         '  overlay:        %s%s' % (managed.overlay_path(ctx.home),
                                     '' if os.path.lexists(managed.overlay_path(ctx.home)) else ' (none)'),
         '  managed file:   %s' % ctx.host.managed_path())
    if not os.path.lexists(managed.overlay_path(ctx.home)):
        emit('WARNING: no overlay found at %s: the file is made from the template '
             'alone, without your own lines (the agents\' git identity, the auto-mode lines that name '
             'you)' % managed.overlay_path(ctx.home))
    return _carry_out('managed-settings', managed.survey(ctx, apply=apply), apply, emit, lines)


class AgentTempContext(object):
    """What the agent temp init alone (tools/agent_temp_init.py) reads: the subset of Context that
    kitlib.machinesetup.agenttemp and the folder's ACL step use."""

    def __init__(self, home, kit, agent_temp=None, host=None, sweep_task=True):
        self.home = os.path.abspath(home)
        self.kit = os.path.abspath(kit)
        self.agent_temp = agent_temp
        self.sweep_task = bool(sweep_task)
        self.host = host or extras.Host()


def survey_agent_temp(ctx):
    """The temp-folder steps of a machine setup alone, in the same order: the folder, AGENT_TEMP,
    the agent_temp_sweep config key and the sweep task (kitlib.machinesetup.agenttemp.survey), then
    the folder's ACL (extras). No links, settings, plugin, shortcut or KIT_MACHINE_LOCK."""
    plan = Plan(('extras',))
    if not os.path.isfile(kit_file(ctx.kit, settings.GUARD)):
        plan.refuse('%s is not a kit checkout (no %s)' % (ctx.kit, settings.GUARD))
        return plan
    folder, made = agenttemp.survey(plan, ctx)
    if folder:
        extras.acl_step(plan, ctx, folder, made)
    return plan


def run_agent_temp(ctx, apply=False, out=None):
    """(exit code, lines) of survey_agent_temp: the plan, and with `apply` its changes, as run()."""
    lines = []

    def emit(*ls):
        for l in ls:
            lines.append(l)
            if out:
                out(l)

    emit('agent-temp-init: %s' % ('APPLY' if apply else 'DRY RUN (nothing changed; add --apply to act)'),
         '  kit:            %s' % ctx.kit,
         '  home:           %s' % ctx.home)
    return _carry_out('agent-temp-init', survey_agent_temp(ctx), apply, emit, lines)


__all__ = ['AgentTempContext', 'Change', 'Context', 'ManagedContext', 'Plan', 'account_dirs', 'dedupe',
           'default_config_dirs', 'run', 'run_agent_temp', 'run_managed', 'run_repo', 'survey',
           'survey_agent_temp']
