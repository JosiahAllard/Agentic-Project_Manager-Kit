"""The agents' temp folder (docs/machine-setup.md "Agent temp init"): the folder itself, the user
environment variable AGENT_TEMP naming it, the machine config's `agent_temp_sweep` key the sweep
(optional/agenttemp/agent_temp_sweep.py) reads, and the scheduled task that runs the sweep. Each is
read first and planned only when it differs. An existing config key or another AGENT_TEMP value is
noted, never overwritten. The task is registered only with `register_sweep_task`; without it a note
carries the PowerShell line that registers it. Nothing here prints or exits.
"""
import json
import os

from kitlib.config import ConfigError
from kitlib.config import user as kuser
from kitlib.worktree.task import NOT_THE_STORE_ALIAS, quoted

from . import settings
from .common import is_link, norm

ENV = 'AGENT_TEMP'
KEY = 'agent_temp_sweep'
MAX_AGE_DAYS = 3
TASK = 'agent-temp-sweep'
AT = '04:00'
SWEEP = os.path.join('optional', 'agenttemp', 'agent_temp_sweep.py')
NEW_LAYOUT = {'bom': False, 'crlf': False, 'final': True, 'indent': 2, 'ascii': False}


def default_agent_temp(kit):
    """`<drive of kit>\\AgentTemp` (kitlib.config.user.TEMP_DIR_NAME), or None for a kit with no
    drive letter (a UNC path). Makes nothing."""
    drive = os.path.splitdrive(os.path.abspath(kit))[0]
    if len(drive) != 2 or drive[1] != ':':
        return None
    return drive + '\\' + kuser.TEMP_DIR_NAME


def task_line(kit):
    """The one PowerShell line that registers TASK: daily at AT as the registering user
    (-RunLevel Limited, -StartWhenAvailable), running `python "<kit>\\SWEEP" --apply` in the kit.
    It throws, registering nothing, when python is missing or is the Microsoft Store alias."""
    kit = os.path.normpath(kit)
    arg = '"%s" --apply' % os.path.join(kit, SWEEP)
    return ("$py = (Get-Command python).Source; %s; Register-ScheduledTask -TaskName %s"
            " -Trigger (New-ScheduledTaskTrigger -Daily -At %s)"
            " -Action (New-ScheduledTaskAction -Execute $py -Argument %s -WorkingDirectory %s)"
            " -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)"
            " -User $env:USERNAME -RunLevel Limited -Description %s"
            % (NOT_THE_STORE_ALIAS, quoted(TASK), AT, quoted(arg), quoted(kit),
               quoted('daily sweep of the agents\' temp folder')))


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


def survey(plan, ctx):
    """Plan the folder, AGENT_TEMP, the config key and the task. (folder, made): the folder the ACL
    step sets up (None when there is none or a step refused), and whether this run makes it."""
    folder, source = choose(ctx)
    if not folder:
        plan.note('extras', 'agent temp init skipped: the kit %s is on no drive letter; pass '
                            '--agent-temp' % ctx.kit)
        return None, False
    if not os.path.isabs(folder):
        plan.refuse('the agent temp folder %s (from %s) is not an absolute path' % (folder, source))
        return None, False
    top = in_checkout(folder)
    if top:
        plan.refuse('the agent temp folder %s is inside the git checkout %s: the sweep refuses to '
                    'sweep it' % (folder, top))
        return None, False
    made = _folder(plan, folder)
    if made is None:
        return None, False
    _env(plan, ctx, folder, source)
    _key(plan, ctx, folder)
    _task(plan, ctx)
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
    try:
        path = kuser.writable_file(ctx.home)
    except ConfigError as e:
        plan.refuse('cannot write %s to the machine config: %s' % (KEY, e))
        return
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except FileNotFoundError:
        raw = None
    except OSError as e:
        plan.refuse('cannot read %s: %s' % (path, e))
        return
    value = {'dir': folder, 'max_age_days': MAX_AGE_DAYS}
    plan.add('extras', 'machine config %s: add %s = %s' % (path, KEY, json.dumps(value)),
             lambda: write_sweep_key(ctx.home, path, raw, folder),
             undo='remove the %s key from %s' % (KEY, path) if raw is not None else 'delete %s' % path)


def write_sweep_key(home, path, raw, folder):
    """Add KEY = {dir: folder, max_age_days: MAX_AGE_DAYS} to the machine config file `path`, whose
    bytes the survey read as `raw` (None: no file), keeping every other key and the file's layout
    (BOM, line ends, indent: kitlib.machinesetup.settings.read). RuntimeError, nothing written, when
    the file changed since the survey, is a link or already has the key; the file is put back when
    the machine config cannot be read after the write."""
    try:
        with open(path, 'rb') as f:
            now = f.read()
    except FileNotFoundError:
        now = None
    if now != raw:
        raise RuntimeError('%s changed since the survey; run again' % path)
    if raw is None:
        layout, data = NEW_LAYOUT, {}
    else:
        if is_link(path) or os.path.islink(path):
            raise RuntimeError('%s is a link; not replaced' % path)
        layout, data = settings.read(raw)
        if not isinstance(data, dict):
            raise RuntimeError('%s holds no JSON object' % path)
    if KEY in data:
        raise RuntimeError('%s already has %s; not overwritten' % (path, KEY))
    data[KEY] = {'dir': folder, 'max_age_days': MAX_AGE_DAYS}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _replace(path, settings.write(layout, data))
    try:
        kuser.user_config(home)
    except ConfigError as e:
        if raw is None:
            os.remove(path)
        else:
            _replace(path, raw)
        raise RuntimeError('the machine config could not be read after adding %s (%s); put back'
                           % (KEY, e))


def _replace(path, data):
    tmp = path + '.machine-setup.tmp'
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def _task(plan, ctx):
    host = ctx.host
    line = task_line(ctx.kit)
    try:
        there = host.task_exists(TASK)
    except (OSError, RuntimeError) as e:
        plan.note('extras', 'could not tell whether the scheduled task %s is registered (%s)' % (TASK, e))
        return
    if there:
        return
    if not ctx.register_sweep_task:
        plan.note('extras', 'scheduled task %s is not registered; the owner registers it with '
                            '--register-sweep-task --apply, which runs: %s' % (TASK, line))
        return
    plan.add('extras', 'register the scheduled task %s (daily %s, as you, not elevated): %s'
             % (TASK, AT, line), lambda: host.task_register(line),
             undo='Unregister-ScheduledTask -TaskName %s -Confirm:$false' % TASK)
