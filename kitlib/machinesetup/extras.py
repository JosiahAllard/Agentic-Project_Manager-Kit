"""Machine extras (docs/machine-setup.md "Extras"): the machine schedule shortcut (in the default mode
only with `shortcut`), the agents' temp
folder with its AGENT_TEMP variable, sweep config key and sweep task (kitlib.machinesetup.agenttemp),
that folder's permissions, and the comfy entry point's lock tool (not in fresh mode: the plugin's
lock tool is used). Each is read first and planned only when it differs; Host is the Windows side (also the
`claude` runner that kitlib.machinesetup.plugin migrates each account with), and tests pass a fake.
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

from kitlib.launcher import pythonw as pythonw_mod

from . import agenttemp, procwatch
from .common import kit_file, norm, under

SHORTCUT = 'Machine schedule.lnk'
LOCK_ENV = 'KIT_MACHINE_LOCK'
SIDS = ('*S-1-5-18', '*S-1-5-32-544')                               # SYSTEM, Administrators
GRANT = '(OI)(CI)F'
_NOWIN = 0x08000000 if sys.platform == 'win32' else 0                # CREATE_NO_WINDOW


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
        if winreg is None:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None

    def env_set(self, name, value):
        self._ps('[Environment]::SetEnvironmentVariable(%s, %s, "User")' % (_ps_quote(name), _ps_quote(value)))

    def task_exists(self, name):
        """Whether the scheduled task `name` is registered (agenttemp.task_registered: False only
        when schtasks says it does not exist; RuntimeError on a timeout or another failure)."""
        return agenttemp.task_registered(name)

    def task_register(self, line):
        """Run the PowerShell line that registers a scheduled task (agenttemp.task_line)."""
        self._ps(line)

    def sweep_dry_run(self, kit, home=None):
        """The sweep's report lines from one run without --apply (agenttemp.sweep_dry_run), under
        this interpreter, reading the machine config of `home`."""
        return agenttemp.sweep_dry_run(kit, sys.executable, home=home)

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
        """Whether this process runs elevated, so it may write under Program Files."""
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
    j = kjob.Job(kill_on_close=False)
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
        _shortcut(plan, ctx)
    else:
        plan.note('extras', 'machine schedule shortcut: not made in the default mode (--shortcut '
                            'makes it)')
    folder, made = agenttemp.survey(plan, ctx)        # before the ACL: it may make the folder
    if folder:
        acl_step(plan, ctx, folder, made)
    procwatch.task_step(plan, ctx, folder)
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
             lambda: host.env_set(LOCK_ENV, want),
             undo=('[Environment]::SetEnvironmentVariable("%s", "%s", "User")' % (LOCK_ENV, have)) if have
             else '[Environment]::SetEnvironmentVariable("%s", $null, "User")' % LOCK_ENV)
    if have and under(have, ctx.old_kit):
        plan.note('extras', '%s pointed into the old kit' % LOCK_ENV)
