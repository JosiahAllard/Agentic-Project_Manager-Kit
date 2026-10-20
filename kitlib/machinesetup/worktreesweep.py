"""The nightly worktree sweep task of every repository in the machine config's launcher.repos
(docs/machine-setup.md "Extras"): `<repo name>-worktree-sweep`, the task
kitlib.worktree.task describes (daily at 03:00 as the registering user, `-RunLevel Limited`,
`-StartWhenAvailable`, base pythonw running the carrier, `--sweep-merged --apply`).

Registered by default, as the agent temp sweep task is: for each repository whose task is missing,
the change first runs the sweep once without --apply (the host's worktree_sweep_dry_run) and prints
what it would remove, then registers. A task already registered is left as is unless it is stale:
a path its action runs lies in a plugin export folder (a 12-hex folder under plugin.export_dir)
that holds no export marker any more (kitlib.allowrules.export_state GONE). A stale task is
registered again with `-Force`. With `worktree_sweep` false (--no-worktree-sweep) a missing or
stale task is a note carrying the line instead.

The task runs the kit's live main checkout (`tool_root`): pluginsource.live_root, else the
checkout an export's marker records, else the kit itself. A kit in a branch worktree, or an export
with neither, registers nothing: the copy is unreviewed, or goes away with a later prune. Two
repositories that take one task name get none. Nothing here prints or exits.
"""
import os
import re
import subprocess

from kitlib import pluginsource
from kitlib.config import ConfigError, load as load_kit_config
from kitlib.git import repo as gitrepo
from kitlib.launcher.pythonw import base_pythonw
from kitlib.proc import spawn
from kitlib.worktree.remove import main_checkout
from kitlib.worktree.task import (AT, NOT_THE_STORE_ALIAS, PYTHONW_IS, argument, description, quoted,
                                  task_name)

from .agenttemp import NOT_A_FILE
from .common import Report

TOOL = os.path.join('tools', 'worktree_remove.py')
DRY_RUN_TIMEOUT = 600                     # a sweep reads every worktree's status: 100 trees take a while
_FOLDER = re.compile(r'^[0-9a-f]{12}$')
EXIT_MEANS = {1: 'refused, failed, or deferred by a held lock: run it again once the lock is free',
              2: 'usage, or a refused .kit.json'}


def tool_root(kit, environ=None):
    """The kit folder whose worktree_remove.py the task runs: the live root a tool pinned to an
    export runs for (pluginsource.live_root), else, for an export, the main checkout its marker
    records (pluginsource.export_checkout), else `kit` itself when it is no export. None for an
    export with neither: a task naming the export runs a folder a later prune removes."""
    kit = os.path.normpath(os.path.abspath(kit))
    live = pluginsource.live_root(kit, environ)
    if os.path.normcase(os.path.normpath(live)) != os.path.normcase(kit):
        return os.path.normpath(live)
    if is_export(kit):
        checkout = pluginsource.export_checkout(kit)
        return os.path.normpath(checkout) if checkout else None
    return kit


def is_export(folder):
    return os.path.isfile(os.path.join(folder, pluginsource.MARKER))


def task_line(main, name, tool, force=False, pythonw=None):
    """The one PowerShell line that registers `<name>-worktree-sweep` for the main checkout `main`,
    running `tool`: kitlib.worktree.task.command's line with agenttemp.NOT_A_FILE added, so it
    throws, registering nothing, when the pythonw is not a file, and with `-Force` when `force`
    (a stale task registered again; PowerShell refuses a name that exists without it)."""
    main = os.path.normpath(main)
    pythonw = pythonw or base_pythonw()
    return ("%s; %s; %s; Register-ScheduledTask%s -TaskName %s"
            " -Trigger (New-ScheduledTaskTrigger -Daily -At %s)"
            " -Action (New-ScheduledTaskAction -Execute $py -Argument %s -WorkingDirectory %s)"
            " -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)"
            " -User $env:USERNAME -RunLevel Limited -Description %s"
            % (PYTHONW_IS % quoted(pythonw), NOT_THE_STORE_ALIAS, NOT_A_FILE, ' -Force' if force else '',
               quoted(task_name(name)), AT, quoted(argument(tool, main)), quoted(main),
               quoted(description(name))))


def dry_run_argv(tool, main, python):
    return [python, os.path.normpath(tool), '-C', os.path.normpath(main), '--sweep-merged']


def dry_run(tool, main, python, timeout=DRY_RUN_TIMEOUT, run=None):
    """Run the sweep once without --apply (it removes nothing) and return its report lines, read
    as UTF-8. RuntimeError when it exits any way but 0 (1: refused, failed or deferred by a held
    lock; 2: usage or a refused .kit.json) or takes over `timeout` seconds, so the task is not
    registered. run: the tests' subprocess.run."""
    run = run or spawn.run
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    try:
        r = run(dry_run_argv(tool, main, python), capture_output=True, encoding='utf-8', errors='replace',
                env=env, timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError('the worktree sweep dry run of %s took over %s s, so the task is not registered'
                           % (os.path.normpath(main), timeout))
    said = [l for l in ((r.stdout or '') + (r.stderr or '')).splitlines() if l.strip()]
    if r.returncode != 0:
        raise RuntimeError('the worktree sweep dry run of %s exit %d (%s), so the task is not registered: %s'
                           % (os.path.normpath(main), r.returncode, EXIT_MEANS.get(r.returncode, 'failed'),
                              ' | '.join(said)[:400] or 'no output'))
    return said


def repo_task(folder):
    """(main checkout, repo name) for the repository at `folder`: the name is `.kit.json`'s
    repo.name, else the main checkout's folder name (as kitlib.worktree.task.resolve names it).
    GitReadError (a RuntimeError) when git cannot find the main checkout; ConfigError for a
    refused .kit.json."""
    main = main_checkout(folder)
    name = load_kit_config(main).repo.get('name') or os.path.basename(os.path.normpath(main))
    return main, name


def find(tasks, name):
    """The task named `name` in the task scheduler's root folder, from consoletasks.parse's list."""
    want = name.lower()
    for t in tasks:
        if (t.get('path') or '').lstrip('\\').lower() == want:
            return t
    return None


def removed_export(task, export_root):
    """The export folder name (12 hex) a path in `task`'s actions runs from when that folder under
    `export_root` holds no export marker (allowrules.export_state GONE); None when none does."""
    from kitlib import allowrules
    from kitlib.pmstart.consoletasks import words
    top = os.path.normcase(os.path.normpath(export_root)).rstrip(os.sep) + os.sep
    current = pluginsource.current_folder(export_root)
    for a in task.get('actions') or []:
        for w in words(a.get('exe') or '') + words(a.get('args') or ''):
            p = os.path.normcase(os.path.normpath(w))
            if not p.startswith(top):
                continue
            folder = p[len(top):].split(os.sep)[0]
            if _FOLDER.match(folder) and allowrules.export_state(export_root, folder, current) == allowrules.GONE:
                return folder
    return None


def survey(plan, ctx):
    """Plan each repository's task in launcher.repos, after the other extras (machinesetup.survey).
    No repository: nothing, not even a note."""
    from kitlib.launcher import settings as launcher
    try:
        repos = launcher.load(ctx.home).repos
    except ConfigError as e:
        plan.note('extras', 'worktree sweep tasks: launcher.repos could not be read (%s); none planned' % e)
        return
    if not repos:
        return
    root = tool_root(ctx.kit)
    if root is None or is_export(root):
        plan.note('extras', 'worktree sweep tasks: the kit at %s is a plugin export with no live main checkout '
                            '(no %s, and its marker records no checkout), so a task would run a folder a later '
                            'prune removes; pass --kit <kit main checkout>; none planned'
                            % (root or ctx.kit, pluginsource.LIVE_ROOT_ENV))
        return
    tool = os.path.join(root, TOOL)
    if not os.path.isfile(tool):
        plan.note('extras', 'worktree sweep tasks: no %s; none planned' % tool)
        return
    place = gitrepo.classify(root)
    if place.kind != gitrepo.MAIN:
        plan.note('extras', 'worktree sweep tasks: the kit at %s is not a main checkout or a plugin export '
                            '(%s), so a task would run an unreviewed copy; none planned'
                            % (root, place.detail or place.kind))
        return
    try:
        export = pluginsource.load(ctx.home, kit=ctx.kit).export_dir
    except (ConfigError, pluginsource.RefreshError) as e:
        export = None
        plan.note('extras', 'worktree sweep tasks: plugin.export_dir could not be read (%s), so a task that '
                            'runs a removed plugin export is not looked for' % e)
    try:
        tasks = ctx.host.task_list()
    except (OSError, RuntimeError, ValueError, SyntaxError) as e:   # ET.ParseError is a SyntaxError
        plan.note('extras', 'worktree sweep tasks: could not read the scheduled tasks (%s); none planned' % e)
        return
    for label, main, name in _unique(plan, _resolved(plan, repos)):
        _repo_step(plan, ctx, label, main, name, tool, tasks, export)


def _resolved(plan, repos):
    """[(label, main checkout, name)] of each launcher repository that can be read; a note for each
    one that cannot."""
    out = []
    for label in sorted(repos):
        folder = repos[label]
        if not os.path.isdir(folder):
            plan.note('extras', 'worktree sweep task skipped for %s: no folder %s' % (label, folder))
            continue
        try:
            main, name = repo_task(folder)
        except (RuntimeError, ConfigError, OSError) as e:
            plan.note('extras', 'worktree sweep task skipped for %s: %s' % (label, e))
            continue
        out.append((label, os.path.normpath(main), name))
    return out


def _unique(plan, resolved):
    """`resolved` with one entry per task name. Labels naming one main checkout are one repository
    (the first label plans it); different checkouts that take one name (the same repo.name, or the
    same remote's last part) share a task name, so none of them gets a task, and a note names them."""
    groups = {}
    for entry in resolved:
        groups.setdefault(task_name(entry[2]).lower(), []).append(entry)
    out = []
    for entry in resolved:
        group = groups[task_name(entry[2]).lower()]
        if entry is not group[0]:
            continue
        mains = sorted(set(os.path.normcase(m) for _, m, _ in group))
        if len(mains) == 1:
            out.append(entry)
            continue
        plan.note('extras', 'worktree sweep task %s skipped: %s take that one name; give each its own '
                            'repo.name in .kit.json and run again'
                  % (task_name(entry[2]), ', '.join('%s (%s)' % (l, m) for l, m, _ in group)))
    return out


def _repo_step(plan, ctx, label, main, name, tool, tasks, export):
    task = task_name(name)
    have = find(tasks, task)
    gone = removed_export(have, export) if have and export else None
    if have and not gone:
        return
    line = task_line(main, name, tool, force=bool(gone))
    was = os.path.join(export, gone) if gone else None
    if not getattr(ctx, 'worktree_sweep', True):
        if gone:
            plan.note('extras', 'worktree sweep task %s (%s) runs from the removed plugin export %s '
                                '(--no-worktree-sweep leaves it); a run without --no-worktree-sweep registers '
                                'it again, or run: %s' % (task, label, was, line))
        else:
            plan.note('extras', 'worktree sweep task %s (%s) is not registered (--no-worktree-sweep); a run '
                                'without --no-worktree-sweep registers it, or run: %s' % (task, label, line))
        return
    host = ctx.host

    def sweep():
        said = host.worktree_sweep_dry_run(tool, main)
        return Report('worktree sweep dry run (nothing removed): ' + l for l in said)

    plan.add('extras', 'run the worktree sweep once without --apply for %s: %s -C %s --sweep-merged'
             % (label, tool, main), sweep,
             detail=['prints what the task\'s first run would remove, before the task is registered; '
                     'stops the run, registering nothing, when the sweep refuses, fails or is deferred'])
    what = ('re-register the scheduled task %s (it runs from the removed plugin export %s; ' % (task, was)
            if gone else 'register the scheduled task %s (' % task)
    plan.add('extras', '%sdaily %s, as you, not elevated): %s' % (what, AT, line),
             lambda: host.task_register(line),
             undo='Unregister-ScheduledTask -TaskName %s -Confirm:$false' % quoted(task))
