"""The agents' temp folder (docs/machine-setup.md "Agent temp init"): the folder itself, the user
environment variable AGENT_TEMP naming it, the machine config's `agent_temp_sweep` key the sweep
(optional/agenttemp/agent_temp_sweep.py) reads, and the scheduled task that runs the sweep. Each is
read first and planned only when it differs. An existing config key or another AGENT_TEMP value is
noted, never overwritten. The task is registered by default: the change first runs the sweep once
without --apply (sweep_dry_run) and prints what it would remove, then registers (task_step, after
the folder's ACL step). With
`sweep_task` false (--no-sweep-task) a note carries the PowerShell line that registers it instead.
A task already registered is left as is. On Linux the task is the pm-kit job TASK
(linux_job; kitlib.platform.linux.schedule: a systemd user timer, else a crontab line) and
task_registered reads it back. Nothing here prints or exits.
"""
import json
import os
import subprocess

from kitlib import platform
from kitlib.config import ConfigError
from kitlib.config import user as kuser
from kitlib.launcher.pythonw import base_pythonw
from kitlib.worktree.task import CARRIER, NOT_THE_STORE_ALIAS, PYTHONW_IS, quoted

from . import machineconfig
from .common import Report, is_link, norm

ENV = 'AGENT_TEMP'
KEY = 'agent_temp_sweep'
MAX_AGE_DAYS = 3
TASK = 'agent-temp-sweep'
AT = '04:00'
SWEEP = os.path.join('optional', 'agenttemp', 'agent_temp_sweep.py')
NOT_A_FILE = ("if (-not (Test-Path -LiteralPath $py -PathType Leaf)) { throw ('no pythonw.exe at ' + $py"
              " + '; nothing was registered') }")


def default_agent_temp(kit):
    """`<drive of kit>\\kit-temp` (kitlib.config.user.TEMP_DIR_NAME), or None for a kit with no
    drive letter (a UNC path). Makes nothing."""
    drive = os.path.splitdrive(os.path.abspath(kit))[0]
    if len(drive) != 2 or drive[1] != ':':
        return None
    return drive + '\\' + kuser.TEMP_DIR_NAME


def task_line(kit, pythonw=None):
    """The one PowerShell line that registers TASK: daily at AT as the registering user
    (-RunLevel Limited, -StartWhenAvailable), running `python "<kit>\\SWEEP" --apply` in the kit
    with no console window: the task's program is pythonw.exe running the carrier
    `<kit>\\tools\\<CARRIER>` (kitlib.proc.spawn.carry), which starts the python.exe beside it.
    `pythonw` is written in as a literal path, default the base install's
    (kitlib.launcher.pythonw.base_pythonw), never a PATH lookup, which finds the Microsoft Store
    alias first. The line is PowerShell text, so it checks for itself: it throws, registering
    nothing, when that pythonw is not a file or is under WindowsApps."""
    kit = os.path.normpath(kit)
    pythonw = pythonw or base_pythonw()
    arg = '"%s" python "%s" --apply' % (os.path.join(kit, 'tools', CARRIER),
                                        os.path.join(kit, SWEEP))
    return ("%s; %s; %s; Register-ScheduledTask -TaskName %s"
            " -Trigger (New-ScheduledTaskTrigger -Daily -At %s)"
            " -Action (New-ScheduledTaskAction -Execute $py -Argument %s -WorkingDirectory %s)"
            " -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)"
            " -User $env:USERNAME -RunLevel Limited -Description %s"
            % (PYTHONW_IS % quoted(pythonw), NOT_THE_STORE_ALIAS, NOT_A_FILE, quoted(TASK), AT, quoted(arg), quoted(kit),
               quoted('daily sweep of the agents\' temp folder')))


def linux_job(kit, python=None):
    """The Linux job of TASK: `python <kit>/SWEEP --apply` in the kit, daily at AT (python:
    default schedule.python())."""
    from kitlib.platform.linux import schedule
    kit = os.path.normpath(kit)
    return schedule.Job(TASK, [python or schedule.python(), os.path.join(kit, SWEEP), '--apply'], kit,
                        ('daily', AT), 'daily sweep of the agents\' temp folder')


TASK_TIMEOUT = 30


def _not_found_text():
    """Windows' localised text for ERROR_FILE_NOT_FOUND (2), which schtasks /Query prints for a
    task that does not exist; None off Windows."""
    try:
        import ctypes
        return ctypes.FormatError(2).strip() or None
    except (ImportError, AttributeError, OSError):
        return None


def task_registered(name, timeout=TASK_TIMEOUT, run=None):
    """Whether the scheduled task `name` is registered (`schtasks /Query /TN name`): True on exit 0,
    False only when schtasks says the task does not exist (its output holds Windows' text for error
    2, file not found). Any other failure (access denied, a timeout after `timeout` seconds) raises
    RuntimeError, never False; OSError when schtasks cannot be run. run: the tests' subprocess.run.
    On Linux: whether the pm-kit job `name` is written (schedule.exists; ScheduleError, a
    RuntimeError, when the crontab cannot be read)."""
    if platform.NAME == 'linux':
        from kitlib.platform.linux import schedule
        return schedule.exists(name, run=run)
    run = run or subprocess.run
    try:
        r = run(['schtasks', '/Query', '/TN', name], capture_output=True, text=True, timeout=timeout,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError('schtasks /Query timed out after %s s' % timeout)
    if r.returncode == 0:
        return True
    said = ' '.join(((r.stderr or '') + ' ' + (r.stdout or '')).split())
    gone = _not_found_text()
    if gone and ' '.join(gone.split()).lower() in said.lower():
        return False
    raise RuntimeError('schtasks /Query exit %d: %s' % (r.returncode, said[:200] or 'no output'))


def _config(home):
    """(the machine config's agent_temp_sweep value or None, why it could not be read or None)."""
    try:
        return kuser.user_config(home).get(KEY), None
    except ConfigError as e:
        return None, str(e)


def choose(ctx):
    """(folder or None, where it came from): --agent-temp, then the config key's dir, then the user
    environment's AGENT_TEMP, then default_agent_temp(kit)."""
    if ctx.agent_temp:
        return os.path.abspath(ctx.agent_temp), '--agent-temp'
    have, _ = _config(ctx.home)
    if isinstance(have, dict) and isinstance(have.get('dir'), str) and have.get('dir'):
        return have['dir'], 'the machine config'
    env = ctx.host.env_get(ENV)
    if env:
        return env, 'the user environment'
    return default_agent_temp(ctx.kit), 'the kit\'s drive'


def in_checkout(path):
    """The folder at or above `path` holding a `.git` entry, or None."""
    p = os.path.abspath(path)
    while True:
        if os.path.lexists(os.path.join(p, '.git')):
            return p
        up = os.path.dirname(p)
        if up == p:
            return None
        p = up


def is_any_link(path):
    """A symlink, a junction or any other reparse point (os.lstat's st_reparse_tag)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return os.path.islink(path) or bool(getattr(st, 'st_reparse_tag', 0)) or is_link(path)


def _forms(path):
    """[path] as written (absolute), then its real path (os.path.realpath: every link or junction
    in it resolved) when that differs."""
    a = os.path.abspath(path)
    try:
        r = os.path.realpath(a)
    except (OSError, ValueError):
        return [a]
    return [a] if norm(r) == norm(a) else [a, r]


def _holds(a, b):
    """a is b or lies under it (normcase, no links resolved)."""
    a, b = norm(a), norm(b)
    return a == b or a.startswith(b.rstrip('\\/') + os.sep)


def unsafe_dir(d, home, kit=None):
    """Why the agents' temp folder `d` may not be swept (optional/agenttemp/agent_temp_sweep.py)
    or set up by machine setup, or None. d is refused when it is a link itself, or when, as written
    or by its real path (os.path.realpath, so a junction in a folder above d cannot point it into a
    repository), it is a drive or filesystem root; the home folder, a folder in it or a folder holding
    it; the kit checkout `kit` or the machine config folder, in one or holding one; or inside a git
    checkout or worktree (a .git folder or file in it or any folder above it). Whether d exists is
    the caller's to check."""
    d = os.path.abspath(d)
    if is_any_link(d):
        return '%s is a link' % d
    h = os.path.abspath(home)
    roots = [('the home folder', h), ('the kit checkout', kit)]
    roots += [('the machine config folder', c) for c in kuser.keep_clear_dirs(h)]
    for p in _forms(d):
        shown = d if p == d else '%s (real path %s)' % (d, p)
        if os.path.dirname(p) == p:
            return '%s is a filesystem root' % shown
        for name, root in roots:
            if not root:
                continue
            for r in _forms(root):
                if _holds(p, r) or _holds(r, p):
                    return '%s is %s %s, in it or holds it' % (shown, name, root)
        top = in_checkout(p)
        if top:
            return '%s is inside a git checkout or worktree (%s has .git)' % (shown, top)
    return None


def survey(plan, ctx):
    """Plan the folder, AGENT_TEMP and the config key (the task is task_step, planned after the ACL
    step, so a sweep dry run that stops the run cannot leave the folder without its ACL). (folder,
    made): the folder the ACL step sets up (None when there is none or a step refused), and whether
    this run makes it. Every source of the folder passes the sweep's refusals (unsafe_dir) first:
    the folder gets the config key and an ACL with inheritance removed."""
    folder, source = choose(ctx)
    if not folder:
        plan.note('extras', 'agent temp init skipped: the kit %s is on no drive letter; pass '
                            '--agent-temp' % ctx.kit)
        return None, False
    if not os.path.isabs(folder):
        plan.refuse('the agent temp folder %s (from %s) is not an absolute path' % (folder, source))
        return None, False
    why = unsafe_dir(folder, ctx.home, ctx.kit)
    if why:
        plan.refuse('the agent temp folder %s (from %s) may not be swept: %s' % (folder, source, why))
        return None, False
    made = _folder(plan, folder)
    if made is None:
        return None, False
    _env(plan, ctx, folder, source)
    _key(plan, ctx, folder)
    return folder, made


def _folder(plan, folder):
    """True when the folder is planned to be made, False when it is there, None on a refusal."""
    if os.path.lexists(folder):
        if is_link(folder) or not os.path.isdir(folder):
            plan.refuse('the agent temp folder %s is a link or not a folder' % folder)
            return None
        return False
    plan.add('extras', 'make the agent temp folder %s' % folder, lambda: os.makedirs(folder),
             undo='remove the empty folder %s' % folder)
    return True


def _env(plan, ctx, folder, source):
    host = ctx.host
    have = host.env_get(ENV)
    if have and norm(have) == norm(folder):
        return
    if have and not ctx.agent_temp:
        plan.note('extras', 'user environment %s is %s, not %s (from %s); left as is (pass '
                            '--agent-temp to set it)' % (ENV, have, folder, source))
        return
    plan.add('extras', 'user environment %s = %s (agents put their temp files there; was %s)'
             % (ENV, folder, have if have else 'unset'),
             lambda: host.env_set(ENV, folder),
             undo=('[Environment]::SetEnvironmentVariable("%s", "%s", "User")' % (ENV, have)) if have
             else '[Environment]::SetEnvironmentVariable("%s", $null, "User")' % ENV)


def _key(plan, ctx, folder):
    have, err = _config(ctx.home)
    if err:
        plan.refuse('cannot read the machine config for %s: %s' % (KEY, err))
        return
    if have is not None:
        d = have.get('dir') if isinstance(have, dict) else None
        if not isinstance(d, str) or norm(d) != norm(folder):
            plan.note('extras', 'the machine config\'s %s.dir is %s, not %s; left as is (the sweep '
                                'sweeps the configured folder)' % (KEY, d, folder))
        return
    snap, why = machineconfig.snapshot(ctx, KEY)
    if snap is None:
        plan.refuse('cannot write %s to the machine config: %s' % (KEY, why))
        return
    path, existed = snap.path, snap.raw is not None
    value = {'dir': folder, 'max_age_days': MAX_AGE_DAYS}
    plan.add('extras', 'machine config %s: add %s = %s' % (path, KEY, json.dumps(value)),
             lambda: write_sweep_key(ctx.home, snap, folder),
             undo='remove the %s key from %s' % (KEY, path) if existed else 'delete %s' % path)


def write_sweep_key(home, snap, folder):
    """Add KEY = {dir: folder, max_age_days: MAX_AGE_DAYS} to the machine config file snap.path
    (kitlib.machinesetup.machineconfig.set_key: every other key and the layout kept; RuntimeError,
    nothing written, when the file changed since the run last read it, is a link or already has
    the key; put back when the machine config cannot be read after the write)."""
    machineconfig.set_key(home, snap, KEY, {'dir': folder, 'max_age_days': MAX_AGE_DAYS})


def sweep_dry_run_argv(kit, python, home=None):
    """The command line of one dry run of the sweep: `python <kit>/SWEEP`, no --apply; with `home`,
    `--home <home>` so it reads the machine config of the home the run sets up."""
    argv = [python, os.path.join(os.path.normpath(kit), SWEEP)]
    return argv + ['--home', home] if home else argv


DRY_RUN_TIMEOUT = 120


def sweep_dry_run(kit, python, timeout=DRY_RUN_TIMEOUT, run=None, home=None):
    """Run the sweep once without --apply (it removes nothing) and return its report lines, so the
    owner sees what the task's first run would remove before it is registered. `home`: the home
    folder whose machine config it reads (the sweep's --home; none: the real one). The sweep prints
    and the lines are read as UTF-8 (PYTHONIOENCODING, errors replaced), not the locale code page,
    so an entry name outside it cannot fail the run. RuntimeError when it refuses (exit 2: no
    config key, a bad config, an unsafe or unlistable folder), exits any way but 0 or 3 (3: the
    process list could not be read, so it would keep every claude/ folder), or takes over `timeout`
    seconds. run: the tests' subprocess.run."""
    run = run or subprocess.run
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    try:
        r = run(sweep_dry_run_argv(kit, python, home), capture_output=True, encoding='utf-8',
                errors='replace', env=env, timeout=timeout,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise RuntimeError('the sweep dry run took over %s s' % timeout)
    said = [l for l in ((r.stdout or '') + (r.stderr or '')).splitlines() if l.strip()]
    if r.returncode not in (0, 3):
        raise RuntimeError('the sweep dry run exit %d, so the task is not registered: %s'
                           % (r.returncode, ' | '.join(said)[:400] or 'no output'))
    return said


def task_step(plan, ctx):
    """Plan the sweep task: the dry run, then the registration. Called after survey and the
    folder's ACL step (extras.survey, extras.acl_step), never before. On Linux: _linux_task_step."""
    if platform.NAME == 'linux':
        return _linux_task_step(plan, ctx)
    host = ctx.host
    line = task_line(ctx.kit)
    try:
        there = host.task_exists(TASK)
    except (OSError, RuntimeError) as e:
        plan.note('extras', 'could not tell whether the scheduled task %s is registered (%s)' % (TASK, e))
        return
    if there:
        if not getattr(ctx, 'sweep_task', True):
            plan.note('extras', 'scheduled task %s is registered; --no-sweep-task leaves it (undo: '
                                'Unregister-ScheduledTask -TaskName %s -Confirm:$false)' % (TASK, TASK))
        return
    if not getattr(ctx, 'sweep_task', True):
        plan.note('extras', 'scheduled task %s is not registered (--no-sweep-task); a run without '
                            '--no-sweep-task registers it, or run: %s' % (TASK, line))
        return

    def dry_run():
        said = host.sweep_dry_run(ctx.kit, ctx.home)
        return Report('sweep dry run (nothing removed): ' + l for l in said)

    plan.add('extras', 'run the sweep once without --apply: %s'
             % ' '.join(sweep_dry_run_argv(ctx.kit, 'python')[1:]), dry_run,
             detail=['prints what the task\'s first run would remove, before the task is registered; '
                     'stops the run, registering nothing, when the sweep refuses'])
    plan.add('extras', 'register the scheduled task %s (daily %s, as you, not elevated): %s'
             % (TASK, AT, line), lambda: host.task_register(line),
             undo='Unregister-ScheduledTask -TaskName %s -Confirm:$false' % TASK)


def _linux_task_step(plan, ctx):
    """task_step on Linux: the same dry run, then the pm-kit job (linuxjobs.plan_job); a job
    already written is left as is, and --no-sweep-task is a note."""
    from kitlib.platform.linux import schedule
    from . import linuxjobs
    host = ctx.host
    try:
        there = host.task_exists(TASK)
    except (OSError, RuntimeError) as e:
        plan.note('extras', 'could not tell whether the pm-kit job %s is written (%s)' % (TASK, e))
        return
    if there:
        if not getattr(ctx, 'sweep_task', True):
            plan.note('extras', 'pm-kit job %s is written; --no-sweep-task leaves it' % TASK)
        return
    job = linux_job(ctx.kit)
    if not getattr(ctx, 'sweep_task', True):
        plan.note('extras', 'pm-kit job %s is not written (--no-sweep-task); a run without '
                            '--no-sweep-task writes it (%s: %s)'
                            % (TASK, schedule.when_text(job.when), ' '.join(job.argv)))
        return

    def dry_run():
        said = host.sweep_dry_run(ctx.kit, ctx.home)
        return Report('sweep dry run (nothing removed): ' + l for l in said)

    plan.add('extras', 'run the sweep once without --apply: %s'
             % ' '.join(sweep_dry_run_argv(ctx.kit, 'python')[1:]), dry_run,
             detail=['prints what the job\'s first run would remove, before the job is written; '
                     'stops the run, writing nothing, when the sweep refuses'])
    linuxjobs.plan_job(plan, ctx, job, 'write')
