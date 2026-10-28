"""Machine extras (docs/machine-setup.md "Extras"): the machine schedule shortcut (in the default mode
only with `shortcut`), the agents' temp
folder with its AGENT_TEMP variable, sweep config key and sweep task (kitlib.machinesetup.agenttemp),
that folder's permissions, the automatic tasks' tick task (AUTO_TASK), and the comfy entry point's
lock tool (not in fresh mode: the plugin's lock tool is used). Each is read first and planned only when it differs; Host is the Windows side (also the
`claude` runner that kitlib.machinesetup.plugin migrates each account with), and tests pass a fake.
On Linux the tasks are pm-kit jobs (kitlib.machinesetup.linuxjobs): Host reads them back and
writes them, and survey adds linger's note. The user environment is ENV_FILE under the profile
(systemd's environment.d, read by the user manager and the sessions it starts), the folder's
permissions are mode 0700 (chmod), and the shortcut is the desktop entry DESKTOP_FILE.
"""
import json
import os
import re
import subprocess
import sys

try:
    import winreg
except ImportError:                                                  # not Windows
    winreg = None

from kitlib import platform
from kitlib.launcher import pythonw as pythonw_mod
from kitlib.platform.linux import schedule
from kitlib.worktree.task import CARRIER, NOT_THE_STORE_ALIAS, PYTHONW_IS, quoted

from . import agenttemp, linuxjobs, procwatch
from .common import Report, kit_file, norm, under

SHORTCUT = 'Machine schedule.lnk'
LOCK_ENV = 'KIT_MACHINE_LOCK'
SIDS = ('*S-1-5-18', '*S-1-5-32-544')                               # SYSTEM, Administrators
GRANT = '(OI)(CI)F'
_NOWIN = 0x08000000 if sys.platform == 'win32' else 0                # CREATE_NO_WINDOW
AUTO_TASK = 'kit-automatic-tasks'
AUTO_EVERY_MINUTES = 15
AUTO_TOOL = os.path.join('tools', 'auto_tasks.py')
ENV_FILE = ('.config', 'environment.d', 'pm-kit.conf')             # Linux, under the profile
DESKTOP_FILE = ('.local', 'share', 'applications', 'pm-kit-machine-schedule.desktop')
PRIVATE = 0o700


def env_file():
    """Linux: the user environment file, ENV_FILE under the profile (password database)."""
    from kitlib.platform.linux import paths
    return os.path.join(paths.profile(), *ENV_FILE)


def env_file_get(path, name):
    """`name`'s value in the environment.d file `path`, or None."""
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return None
    for l in lines:
        k, eq, v = l.partition('=')
        if eq and k.strip() == name:
            return v.strip() or None
    return None


def env_file_set(path, name, value):
    """Set `name` to `value` in the environment.d file `path` (its line replaced, or appended),
    every other line kept; written through a temp file and os.replace, its folder made."""
    if '\n' in value or '\r' in value:
        raise ValueError('a value with a line break cannot go in %s' % path)
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    out, done = [], False
    for l in lines:
        k, eq, _ = l.partition('=')
        if eq and k.strip() == name:
            if not done:
                out.append('%s=%s' % (name, value))
                done = True
            continue
        out.append(l)
    if not done:
        out.append('%s=%s' % (name, value))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(out) + '\n')
    os.replace(tmp, path)


def env_undo(name, have):
    """The by-hand restore of the user environment variable `name`, `have` its old value."""
    if platform.NAME == 'linux':
        where = '~/' + '/'.join(ENV_FILE)
        return ('set %s=%s in %s' % (name, have, where)) if have else 'delete the %s line in %s' % (name, where)
    return ('[Environment]::SetEnvironmentVariable("%s", "%s", "User")' % (name, have)) if have \
        else '[Environment]::SetEnvironmentVariable("%s", $null, "User")' % name


def _ps_quote(s):
    return "'" + s.replace("'", "''") + "'"


class Host(object):
    """The real machine: PowerShell for shortcuts and the user environment, icacls, the guard's
    settings entry (boot/hook.py), the `claude` runner."""

    def _ps(self, script):
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                           capture_output=True, text=True, creationflags=_NOWIN)
        if r.returncode != 0:
            raise RuntimeError('powershell exit %d: %s' % (r.returncode, (r.stderr or r.stdout).strip()))
        return r.stdout

    def desktop(self):
        return self._ps('[Environment]::GetFolderPath("Desktop")').strip()

    def read_shortcut(self, path):
        """{'target', 'args', 'workdir'} of a .lnk, or None when there is none."""
        if not os.path.isfile(path):
            return None
        out = self._ps('$l = (New-Object -ComObject WScript.Shell).CreateShortcut(%s); '
                       '@{target=$l.TargetPath; args=$l.Arguments; workdir=$l.WorkingDirectory} '
                       '| ConvertTo-Json -Compress' % _ps_quote(path))
        return json.loads(out)

    def write_shortcut(self, path, target, args, workdir, description):
        self._ps('$l = (New-Object -ComObject WScript.Shell).CreateShortcut(%s); $l.TargetPath = %s; '
                 '$l.Arguments = %s; $l.WorkingDirectory = %s; $l.Description = %s; $l.Save()'
                 % tuple(_ps_quote(x) for x in (path, target, args, workdir, description)))

    def icacls(self, args):
        r = subprocess.run(['icacls'] + list(args), capture_output=True, text=True, creationflags=_NOWIN)
        if r.returncode != 0:
            raise RuntimeError('icacls exit %d: %s' % (r.returncode, (r.stdout + r.stderr).strip()))
        return r.stdout

    def user(self):
        return '%s\\%s' % (os.environ.get('USERDOMAIN', ''), os.environ.get('USERNAME', ''))

    def env_get(self, name):
        if platform.NAME == 'linux':
            return env_file_get(env_file(), name)
        if winreg is None:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None

    def env_set(self, name, value):
        if platform.NAME == 'linux':
            return env_file_set(env_file(), name, value)
        self._ps('[Environment]::SetEnvironmentVariable(%s, %s, "User")' % (_ps_quote(name), _ps_quote(value)))

    def user_path(self):
        """(raw value, winreg type) of the user Path (HKCU\\Environment), %VAR% unexpanded; (None,
        None) when it is unset. Read only. Not env_get's expanded read, so a write keeps it."""
        if winreg is None:
            return None, None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as k:
                value, kind = winreg.QueryValueEx(k, 'Path')
        except FileNotFoundError:
            return None, None
        if not isinstance(value, str):
            raise ValueError('HKCU\\Environment Path is not a string (type %d)' % kind)
        return value, kind

    def set_user_path(self, value, kind):
        """Write the user Path through winreg as `kind` (REG_EXPAND_SZ keeps %VAR% entries; .NET's
        SetEnvironmentVariable would write REG_SZ), then broadcast WM_SETTINGCHANGE so programs
        started later from Explorer see it. A Report line when the broadcast fails."""
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, 'Path', 0, kind, value)
        try:
            import ctypes
            from ctypes import wintypes
            result = wintypes.DWORD(0)
            # HWND_BROADCAST, WM_SETTINGCHANGE, "Environment", SMTO_ABORTIFHUNG, 5 s
            ok = ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, 'Environment', 0x0002, 5000,
                                                          ctypes.byref(result))
        except (ImportError, AttributeError, OSError) as e:
            return Report(['the PATH change was not broadcast (%s): sign out and in to see it' % e])
        if not ok:
            return Report(['the PATH change was not broadcast: sign out and in to see it'])
        return None

    def task_exists(self, name):
        """Whether the scheduled task `name` is registered (agenttemp.task_registered: False only
        when schtasks says it does not exist; RuntimeError on a timeout or another failure)."""
        return agenttemp.task_registered(name)

    def task_register(self, line):
        """Run the PowerShell line that registers a scheduled task (agenttemp.task_line)."""
        self._ps(line)

    def job_backend(self, job):
        """Linux: the backend the pm-kit job `job` goes to (schedule.choose: ScheduleError when
        there is none)."""
        return schedule.choose(job)

    def job_describe(self, job, kind):
        """Linux: the lines saying what job_install(job) writes (schedule.describe)."""
        return schedule.describe(job, kind)

    def job_install(self, job):
        """Linux: write and enable the pm-kit job `job` (schedule.install); its lines."""
        return Report(schedule.install(job))

    def systemd(self):
        """Linux: whether the user's systemd manager answers."""
        return schedule.has_systemd()

    def linger(self):
        """Linux: whether linger is on for this user (True, False, None: unknown)."""
        return schedule.linger()

    def sweep_dry_run(self, kit, home=None):
        """The sweep's report lines from one run without --apply (agenttemp.sweep_dry_run), under
        this interpreter, reading the machine config of `home`."""
        return agenttemp.sweep_dry_run(kit, sys.executable, home=home)

    def task_list(self):
        """Every scheduled task with its actions (kitlib.pmstart.consoletasks.read_tasks):
        RuntimeError, OSError or ET.ParseError when schtasks cannot be read. On Linux every
        pm-kit job read back."""
        from kitlib.pmstart import consoletasks
        return consoletasks.read_tasks(timeout=agenttemp.TASK_TIMEOUT)

    def worktree_sweep_dry_run(self, tool, main):
        """The worktree sweep's report lines from one run without --apply on the main checkout
        `main` (worktreesweep.dry_run), under this interpreter."""
        from . import worktreesweep
        return worktreesweep.dry_run(tool, main, sys.executable)

    def guard_entry(self, kit):
        """The guard's settings entry, as the kit's hook entry prints it for the default host:
        `boot/hook.py <host> --settings-entry`, run by this interpreter."""
        from kitlib.host import registry
        r = subprocess.run([sys.executable, kit_file(kit, 'boot/hook.py'), registry.default(),
                            '--settings-entry'], capture_output=True, text=True, creationflags=_NOWIN)
        if r.returncode != 0:
            raise RuntimeError('boot/hook.py --settings-entry exit %d: %s' % (r.returncode, r.stderr.strip()))
        entry = json.loads(r.stdout)
        if not isinstance(entry, dict) or not all(isinstance(v, list) for v in entry.values()):
            raise ValueError('boot/hook.py --settings-entry printed no {event: [groups]} object')
        return entry

    def managed_path(self):
        """The default host's machine-wide managed settings file (kitlib.machinesetup.managed):
        its adapter's optional managed_settings_path(); None when the adapter has none, or names
        no file on this machine."""
        import importlib
        from kitlib.host import registry
        try:
            adapter = importlib.import_module(registry.host(registry.default()).adapter).ADAPTER
        except (ImportError, AttributeError, OSError, ValueError):     # registry.RegistryError
            return None
        get = getattr(adapter, 'managed_settings_path', None)
        return get() if get is not None else None

    def is_admin(self):
        """Whether this process runs elevated, so it may write under Program Files (on Linux:
        as root)."""
        if platform.NAME == 'linux':
            return os.geteuid() == 0
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (ImportError, AttributeError, OSError):
            return False

    def git_email(self, kit):
        """`git config user.email` in the kit checkout (the user's global value unless the kit
        checkout sets its own), or None when it is unset or git cannot run."""
        from kitlib.git import cmd
        r = cmd.call(['config', 'user.email'], kit)
        if r.rc != 0:
            return None
        return cmd.text(r.out or b'').strip() or None

    def plugin_runner(self, home, claude=None, timeout=None, environ=None):
        """The `claude` runner of the plugin step: kitlib.pluginsource.claude.Runner (the claude on
        PATH unless `claude` names one; CLAUDE_CONFIG_DIR set to the folder, removed for ~/.claude,
        by its env_for), with run_tree as its subprocess.run."""
        from kitlib.pluginsource import claude as pclaude
        return pclaude.Runner(home, claude=claude, run_proc=run_tree,
                              timeout=timeout or pclaude.TIMEOUT, environ=environ)


KILL_GRACE = 10


def run_tree(cmd, timeout=None, **kw):
    """subprocess.run for kitlib.pluginsource.claude.Runner, the command's whole process tree in a
    job (kitlib.proc.job.Job): past `timeout` the job ends every process in it, then
    subprocess.TimeoutExpired. subprocess.run kills only its direct child, then reads the pipes to
    their end: a claude installed through npm is a .cmd shim, so that child is cmd.exe and node is
    its child, which holds the pipe open and keeps run waiting for it. Returns CompletedProcess."""
    from kitlib.proc import job as kjob
    j = kjob.Job(kill_on_close=False, short=True)
    try:
        p = j.start(cmd, **kw)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            j.terminate()
            try:
                p.communicate(timeout=KILL_GRACE)
            except subprocess.TimeoutExpired:
                pass
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, p.returncode, out, err)
    finally:
        j.close()


def pythonw():
    """The windowless python of this interpreter's base install (kitlib.launcher.pythonw), never
    a venv's or the Store alias; this interpreter when that is not a usable file."""
    w = pythonw_mod.base_pythonw()
    return sys.executable if pythonw_mod.refusal(w) else w


def survey(plan, ctx):
    if getattr(ctx, 'shortcut', True):
        if platform.NAME == 'linux':
            desktop_entry(plan, ctx)
        else:
            _shortcut(plan, ctx)
    else:
        plan.note('extras', 'machine schedule shortcut: not made in the default mode (--shortcut '
                            'makes it)')
    folder, made = agenttemp.survey(plan, ctx)        # before the ACL: it may make the folder
    if folder:
        acl_step(plan, ctx, folder, made)
    procwatch.task_step(plan, ctx, folder)
    auto_task_step(plan, ctx)
    if platform.NAME == 'linux':
        linuxjobs.linger_note(plan, ctx)
    if ctx.fresh:
        plan.note('extras', '%s left as is in fresh mode: the plugin\'s lock tool is used' % LOCK_ENV)
    else:
        _comfy(plan, ctx)


def acl_step(plan, ctx, folder, made=False):
    """The agent temp folder's ACL step, then the sweep task (agenttemp.task_step): the task's dry
    run can stop the run, so it is planned after the ACL hardening. Also called alone by
    kitlib.machinesetup.survey_agent_temp."""
    _acl(plan, ctx, folder, made)
    agenttemp.task_step(plan, ctx)


def auto_argument(root):
    """The tick task's argument string: the carrier, `python`, tools/auto_tasks.py, `tick`."""
    root = os.path.normpath(root)
    return '"%s" python "%s" tick' % (os.path.join(root, 'tools', CARRIER), os.path.join(root, AUTO_TOOL))


def auto_task_line(root, pythonw=None):
    """The one PowerShell line that registers AUTO_TASK (-Force: replaces one already there), every
    AUTO_EVERY_MINUTES minutes as the registering user (-RunLevel Limited), one run at a time, ended
    after 10 minutes: `pythonw` (a literal path, default the base install's; no window) running the
    carrier, which runs tools/auto_tasks.py tick of the kit at `root` with the python.exe beside it.
    The line throws, registering nothing, when that pythonw is not a file or is under WindowsApps."""
    root = os.path.normpath(root)
    pythonw = pythonw or pythonw_mod.base_pythonw()
    return ("%s; %s; %s; Register-ScheduledTask -Force -TaskName %s"
            " -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date)"
            " -RepetitionInterval (New-TimeSpan -Minutes %d) -RepetitionDuration (New-TimeSpan -Days 3650))"
            " -Action (New-ScheduledTaskAction -Execute $py -Argument %s -WorkingDirectory %s)"
            " -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew"
            " -ExecutionTimeLimit (New-TimeSpan -Minutes 10))"
            " -User $env:USERNAME -RunLevel Limited -Description %s"
            % (PYTHONW_IS % quoted(pythonw), NOT_THE_STORE_ALIAS, agenttemp.NOT_A_FILE, quoted(AUTO_TASK),
               AUTO_EVERY_MINUTES, quoted(auto_argument(root)), quoted(root),
               quoted('kit automatic tasks: starts what the Action Hub schedules (auto_tasks.py tick)')))


def auto_task_same(task, pythonw, argument):
    """Whether the registered `task` (consoletasks' form) runs `pythonw` with `argument` already."""
    exe = os.path.normcase(os.path.normpath(pythonw))
    for a in (task or {}).get('actions') or ():
        have = os.path.normcase(os.path.normpath((a.get('exe') or '').strip().strip('"') or '.'))
        if have == exe and (a.get('args') or '').strip() == argument:
            return True
    return False


def auto_job(root, python=None):
    """The Linux job of AUTO_TASK: `python <root>/AUTO_TOOL tick` in `root`, every
    AUTO_EVERY_MINUTES minutes, ended after 10."""
    root = os.path.normpath(root)
    return schedule.Job(AUTO_TASK, [python or schedule.python(), os.path.join(root, AUTO_TOOL), 'tick'],
                        root, ('every', AUTO_EVERY_MINUTES),
                        'kit automatic tasks: starts what the Action Hub schedules', 10)


def auto_task_step(plan, ctx):
    """Plan AUTO_TASK, the tick of the hub's automatic tasks, for the kit's live main checkout
    (worktreesweep.tool_root, as the worktree sweep tasks take it): registered when missing,
    registered again (-Force) when it runs another program or argument, nothing when it is already
    as planned. A kit with no such checkout, or no tools/auto_tasks.py, is a note."""
    from kitlib.git import repo as gitrepo
    from . import worktreesweep
    root = worktreesweep.tool_root(ctx.kit)
    if root is None or worktreesweep.is_export(root):
        plan.note('extras', '%s task: the kit at %s is a plugin export with no live main checkout; '
                            'not planned' % (AUTO_TASK, root or ctx.kit))
        return
    if not os.path.isfile(os.path.join(root, AUTO_TOOL)):
        plan.note('extras', '%s task: no %s; not planned' % (AUTO_TASK, os.path.join(root, AUTO_TOOL)))
        return
    place = gitrepo.classify(root)
    if place.kind != gitrepo.MAIN:
        plan.note('extras', '%s task: the kit at %s is not a main checkout (%s), so the task would run an '
                            'unreviewed copy; not planned' % (AUTO_TASK, root, place.detail or place.kind))
        return
    linux = platform.NAME == 'linux'
    if not linux:
        pythonw = pythonw_mod.base_pythonw()
        argument = auto_argument(root)
    try:
        tasks = ctx.host.task_list()
    except (OSError, RuntimeError, ValueError, SyntaxError) as e:   # ET.ParseError is a SyntaxError
        plan.note('extras', '%s task: could not read the scheduled tasks (%s); not planned' % (AUTO_TASK, e))
        return
    have = worktreesweep.find(tasks, AUTO_TASK)
    if linux:
        job = auto_job(root)
        if not schedule.same(have, job):
            linuxjobs.plan_job(plan, ctx, job, 'update' if have else 'write')
        return
    if have and auto_task_same(have, pythonw, argument):
        return
    line = auto_task_line(root, pythonw)
    plan.add('extras', '%s the scheduled task %s (every %d min, as you, not elevated, no window; starts what '
                       'the Action Hub schedules): %s'
             % ('update' if have else 'register', AUTO_TASK, AUTO_EVERY_MINUTES, line),
             lambda: ctx.host.task_register(line),
             undo='Unregister-ScheduledTask -TaskName "%s" -Confirm:$false' % AUTO_TASK)


def _shortcut(plan, ctx):
    host = ctx.host
    try:
        desk = ctx.desktop or host.desktop()
    except (OSError, RuntimeError) as e:
        plan.note('extras', 'machine schedule shortcut skipped: no desktop folder (%s)' % e)
        return
    if not desk or not os.path.isdir(desk):
        plan.note('extras', 'machine schedule shortcut skipped: no desktop folder at %s' % desk)
        return
    path = os.path.join(desk, SHORTCUT)
    want = {'target': pythonw(), 'args': '"%s"' % os.path.join(ctx.kit, 'reserve_ui.pyw'),
            'workdir': ctx.kit}
    try:
        have = host.read_shortcut(path)
    except (OSError, RuntimeError, ValueError) as e:
        plan.refuse('cannot read %s: %s' % (path, e))
        return
    if not have or norm(have.get('target') or '.') != norm(want['target']) \
            or (have.get('args') or '') != want['args'] \
            or norm(have.get('workdir') or '.') != norm(want['workdir']):
        plan.add('extras', '%s shortcut %s: %s %s (in %s)'
                 % ('write the' if have is None else 'rewrite the', path, want['target'], want['args'], want['workdir']),
                 lambda: host.write_shortcut(path, want['target'], want['args'], want['workdir'],
                                             'The machine schedule: reserve or release the machine'),
                 undo=('delete "%s"' % path) if have is None else
                 'set it back to %s %s (in %s)' % (have.get('target'), have.get('args'), have.get('workdir')))
    for n in sorted(os.listdir(desk)):
        if not n.lower().endswith('.lnk') or n == SHORTCUT:
            continue
        try:
            other = host.read_shortcut(os.path.join(desk, n)) or {}
        except (OSError, RuntimeError, ValueError):
            continue
        text = ' '.join(str(other.get(k) or '') for k in ('target', 'args', 'workdir'))
        old = ctx.old_kit and norm(ctx.old_kit) in os.path.normcase(text.replace('/', '\\'))
        if old or 'reserve_shortcut.ps1' in text.lower():
            plan.note('extras', 'desktop shortcut %s still runs %s; left as is (delete it by hand once '
                                '"%s" works)' % (n, text.strip(), SHORTCUT[:-4]))


def _acl(plan, ctx, folder, made=False):
    """`made`: this run makes the folder (agenttemp.survey), so there are no permissions to read
    yet; the grant is planned as for a folder that inherits the drive's."""
    if not made and not os.path.isdir(folder):
        plan.refuse('the agent temp folder %s does not exist' % folder)
        return
    if platform.NAME == 'linux':
        _chmod(plan, folder, made)
        return
    host = ctx.host
    me = host.user()
    dom, _, name = me.partition('\\')
    if not dom or not name:
        plan.refuse('cannot name the user for %s\'s permissions: USERDOMAIN or USERNAME is empty (%r)' % (folder, me))
        return
    grant = [folder, '/inheritance:r', '/grant:r'] + ['%s:%s' % (s, GRANT) for s in SIDS] + ['%s:%s' % (me, GRANT)]
    if made:
        plan.add('extras', '%s (made by this run): permissions to SYSTEM, Administrators and %s only '
                           '(full control), inheritance from the drive removed: icacls %s'
                 % (folder, me, ' '.join('"%s"' % a for a in grant)),
                 lambda: host.icacls(grant), undo='icacls "%s" /reset' % folder)
        return
    try:
        now = host.icacls([folder])
    except (OSError, RuntimeError) as e:
        plan.refuse('cannot read the permissions of %s: %s' % (folder, e))
        return
    if acl_done(now, folder, me):
        return
    removes = [[folder, '/remove:d' if deny else '/remove:g', who] for who, deny in acl_others(now, folder, me)]
    cmds = [grant] + removes
    text = '%s: permissions to SYSTEM, Administrators and %s only (full control), inheritance from the ' \
           'drive removed' % (folder, me)
    if removes:
        text += ', other explicit entries removed (%s)' % ', '.join(r[2] for r in removes)
    plan.add('extras', text + ': ' + ' then '.join('icacls ' + ' '.join('"%s"' % a for a in c) for c in cmds),
             lambda: [host.icacls(c) for c in cmds], undo='icacls "%s" /reset' % folder,
             detail=['now: ' + l for l in acl_lines(now, folder)])


def _chmod(plan, folder, made):
    """Linux: the agent temp folder private to this user (mode PRIVATE), planned when it is not."""
    mode = None
    if not made:
        try:
            mode = os.stat(folder).st_mode & 0o777
        except OSError as e:
            plan.refuse('cannot read the permissions of %s: %s' % (folder, e))
            return
        if mode == PRIVATE:
            return
    plan.add('extras', '%s%s: mode %o, yours only: chmod %o "%s"'
             % (folder, ' (made by this run)' if made else '', PRIVATE, PRIVATE, folder),
             lambda: os.chmod(folder, PRIVATE),
             undo=None if made else 'chmod %o "%s"' % (mode, folder))


def desktop_text(kit, python):
    """The machine schedule's desktop entry: `python` running reserve_ui.pyw in the kit."""
    return '\n'.join(['[Desktop Entry]', 'Type=Application', 'Name=Machine schedule',
                      'Comment=The machine schedule: reserve or release the machine',
                      'Exec="%s" "%s"' % (python, os.path.join(kit, 'reserve_ui.pyw')),
                      'Path=%s' % kit, 'Terminal=false', ''])


def desktop_entry(plan, ctx):
    """Linux's shortcut: DESKTOP_FILE under the home folder, written when it differs."""
    path = os.path.join(ctx.home, *DESKTOP_FILE)
    want = desktop_text(ctx.kit, sys.executable)
    try:
        with open(path, encoding='utf-8') as f:
            have = f.read()
    except FileNotFoundError:
        have = None
    except (OSError, ValueError) as e:
        plan.refuse('cannot read %s: %s' % (path, e))
        return
    if have == want:
        return

    def write():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(want)

    plan.add('extras', '%s the desktop entry %s: %s reserve_ui.pyw (in %s)'
             % ('write' if have is None else 'rewrite', path, sys.executable, ctx.kit), write,
             undo=('delete "%s"' % path) if have is None else 'its old text was: %r' % have)


def acl_lines(out, folder):
    lines = []
    for l in out.splitlines():
        l = l.strip()
        if not l or l.lower().startswith('successfully processed') or l.lower().startswith('failed processing'):
            continue
        if l.lower().startswith(folder.lower()):
            l = l[len(folder):].strip()
        lines.append(l)
    return lines


def _entries(out, folder):
    """(who, rights) per access entry in English icacls output."""
    for l in acl_lines(out, folder):
        i = l.find(':(')
        if i >= 0:
            yield l[:i].strip(), l[i + 1:]


def _allowed(me):
    return {'nt authority\\system', 'builtin\\administrators', me.lower()}


def acl_done(out, folder, me):
    """True when nothing is inherited and only SYSTEM, Administrators and `me` hold full control."""
    names = set()
    for who, rights in _entries(out, folder):
        if '(I)' in rights or '(F)' not in rights or '(DENY)' in rights:
            return False
        names.add(who.lower())
    return bool(names) and names <= _allowed(me) and me.lower() in names


def acl_others(out, folder, me):
    """[(who, deny)]: the explicit (not inherited) entries /inheritance:r /grant:r leaves in place:
    any for someone but SYSTEM, Administrators and `me`, and any deny entry. An unresolved SID is
    given as *S-..."""
    seen, out_ = set(), []
    for who, rights in _entries(out, folder):
        if '(I)' in rights or (who.lower() in _allowed(me) and '(DENY)' not in rights):
            continue                                                 # a deny entry goes for anyone
        key = (who.lower(), '(DENY)' in rights)
        if key in seen:
            continue
        seen.add(key)
        out_.append(('*' + who if re.match(r'^S-1-[\d-]+$', who) else who, '(DENY)' in rights))
    return out_


def _comfy(plan, ctx):
    want = os.path.join(ctx.kit, 'tools', 'machine_lock.py')
    host = ctx.host
    have = host.env_get(LOCK_ENV)
    if have and norm(have) == norm(want):
        return
    plan.add('extras', 'user environment %s = %s (comfy_gen finds the lock tool through it; was %s)'
             % (LOCK_ENV, want, have if have else 'unset'),
             lambda: host.env_set(LOCK_ENV, want), undo=env_undo(LOCK_ENV, have))
    if have and under(have, ctx.old_kit):
        plan.note('extras', '%s pointed into the old kit' % LOCK_ENV)
