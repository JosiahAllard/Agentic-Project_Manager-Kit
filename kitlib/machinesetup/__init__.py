"""kitlib.machinesetup: switch a machine's Claude config folders from an older kit clone to this
kit (tools/machine_setup.py, installer/machine-setup.ps1; docs/machine-setup.md).

survey() reads the machine and returns a Plan: the changes to make, in order, each with the text a
dry run prints and the function --apply runs, plus the refusals that stop both. Three parts:
- links (kitlib.machinesetup.links): `tools`, `knowledge/asset-generation` and the skill links
  that point into the old kit clone become directory junctions into this kit;
- settings (kitlib.machinesetup.settings): each config folder's settings.json loses the old kit's
  PreToolUse hooks, gains guard.py's entry and has its old hook and statusline paths re-pointed;
- extras (kitlib.machinesetup.extras): the machine schedule shortcut, the agents' temp folder ACL,
  and KIT_MACHINE_LOCK for comfy.
Nothing here prints or exits; run() gives the exit code and the lines.
"""
import datetime
import os

from . import extras, links, settings
from .common import Change, Plan

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2


class Context(object):
    """What a survey reads. `host` is the Windows side (extras.Host): shortcuts, icacls, the user
    environment and guard.py's settings entry; tests pass a fake."""

    def __init__(self, home, kit, old_kit, config_dirs=None, agent_temp=None, desktop=None,
                 host=None, today=None):
        self.home = os.path.abspath(home)
        self.kit = os.path.abspath(kit)
        self.old_kit = os.path.abspath(old_kit)
        self.config_dirs = dedupe(os.path.abspath(d) for d in (config_dirs or default_config_dirs(self.home)))
        self.agent_temp = agent_temp
        self.desktop = desktop
        self.host = host or extras.Host()
        self.today = today or datetime.date.today().strftime('%Y%m%d')


def default_config_dirs(home):
    """`<home>/.claude` and every `<home>/.claude-*` folder, the kit's own machine folder excepted."""
    out = []
    try:
        names = sorted(os.listdir(home))
    except OSError:
        return out
    for n in names:
        if (n == '.claude' or n.startswith('.claude-')) and n != '.claude-pm-kit' \
                and os.path.isdir(os.path.join(home, n)):
            out.append(os.path.join(home, n))
    return out


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
    plan = Plan()
    if not os.path.isfile(os.path.join(ctx.kit, 'claude', 'hooks', 'guard.py')):
        plan.refuse('%s is not a kit checkout (no claude/hooks/guard.py)' % ctx.kit)
        return plan
    if os.path.normcase(ctx.kit) == os.path.normcase(ctx.old_kit):
        plan.refuse('the old kit and the kit are the same folder: %s' % ctx.kit)
        return plan
    for d in ctx.config_dirs:
        if not os.path.isdir(d):
            plan.refuse('no config folder at %s' % d)
    if plan.refusals:
        return plan
    for d in ctx.config_dirs:
        links.survey(plan, d, ctx)
    for d in ctx.config_dirs:
        settings.survey(plan, d, ctx)
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
         '  old kit:        %s' % ctx.old_kit,
         '  config folders: %s' % ', '.join(ctx.config_dirs))
    plan = survey(ctx)
    emit(*plan.lines())
    if plan.refusals:
        emit(*['machine-setup: REFUSED - %s' % r for r in plan.refusals])
        emit('machine-setup: nothing changed')
        return EXIT_REFUSED, lines
    if not plan.changes:
        emit('machine-setup: nothing to do')
        return EXIT_OK, lines
    if not apply:
        emit('machine-setup: %d change(s) to make; re-run with --apply to make them' % len(plan.changes))
        return EXIT_OK, lines
    done = []
    for c in plan.changes:
        try:
            c.do()
        except BaseException as e:                                   # any failure gets the report
            emit('machine-setup: STOPPED at: %s' % c.text,
                 '  error: %s: %s' % (type(e).__name__, e))
            if c.undo:
                emit('  restore: %s' % c.undo)
            emit('  already done by this run (%d): %s' % (len(done), '; '.join(done) or 'none'))
            if not isinstance(e, Exception):                         # Ctrl+C, exit: report, then go on
                raise
            return EXIT_REFUSED, lines
        done.append(c.text)
        emit('done: %s' % c.text)
    emit('machine-setup: made %d change(s); a second run reports nothing to do' % len(done))
    return EXIT_OK, lines


__all__ = ['Change', 'Context', 'Plan', 'dedupe', 'default_config_dirs', 'run', 'survey']
