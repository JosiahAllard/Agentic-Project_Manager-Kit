"""The --delegation mode (docs/linux-support-plan.md sections 2.3 and 6, slice 14;
docs/machine-setup.md "Linux"): the one root step that gives strong containment (S1) on a systemd
box, or a boot hook on a box with no systemd, offered by the default mode below S1 and run by the
owner with sudo. Declining it is allowed: the box is then at the lower tier, shown as such. After
it the kit never asks for root again.

On systemd, its changes, each planned only when it differs:
- DROPIN, a drop-in for every user's systemd manager (`user@.service.d`) delegating the cpuset,
  cpu, io, memory and pids controllers (DROPIN_TEXT), then `systemctl daemon-reload`. The user
  manager reads it when it starts again: sign out and in (or reboot) once.

With no systemd (boot_route: cgroup v2, then OpenRC's local.d, else a cron.d @reboot line; runit
and s6 are refused), the boot hook:
- SCRIPT (0755), a fixed sh script with the kit user's uid, gid and home written in
  (boot_script): it makes the cgroup /pm-kit with the leaf /pm-kit/anchor, turns the controllers
  on, gives the two folders and their delegatable files to the kit user, moves itself into the
  anchor and, dropped to the kit user, starts the kit's tmux server (`tmux -L pm-kit`) there, its
  pm-kit-anchor session holding HOLD (no shell to `exit`) and the server outliving its last
  session. Work started from that server is contained (kitlib.platform.linux.contain.
  delegated_group); ATTACH is the way in.
- LOCALD (OpenRC, 0755; `rc-update add local default` when local is in no runlevel) or CROND
  (0644), which runs SCRIPT at boot; root's own crontab is never touched.
- one run of SCRIPT now, so no reboot is needed; and again on a later run when the server or its
  anchor session has gone (anchor_down), the only way back short of a reboot.
Every binary it runs and every path it writes must be root's alone, through no link; tmux must be
3.2 or later (the launcher's new-session -e).

Both routes write linuxbox.MARKER, the box's one kit user (`<uid> <name>`): the user who ran sudo.
With it, the default mode refuses any other user (plan question 10).

Refused: off Linux; in WSL; no systemd and no boot route; a MARKER naming another user. --apply
needs root, through sudo from the kit user's account (SUDO_UID names the user; root's own login is
refused). Nothing here prints or exits.
"""
import os
import re
import stat as statmod
import subprocess

from kitlib import platform
from kitlib.proc import spawn

from . import linuxbox
from .common import Plan, make_folder

SECTION = 'delegation'
DROPIN = '/etc/systemd/system/user@.service.d/pm-kit-delegate.conf'
DROPIN_TEXT = ('# pm-kit: written by tools/machine_setup.py --delegation; delete this file and run\n'
               '# systemctl daemon-reload to undo\n'
               '[Service]\n'
               'Delegate=cpuset cpu io memory pids\n')
RELOAD = ['systemctl', 'daemon-reload']

# the boot hook (off systemd)
SCRIPT = '/etc/pm-kit/cgroup-boot'
LOCALD = '/etc/local.d/pm-kit-cgroup.start'
CROND = '/etc/cron.d/pm-kit'
CGROUP = '/sys/fs/cgroup/pm-kit'
OPENRC_RUN = '/run/openrc'
LOCALD_DIR = '/etc/local.d'
CROND_DIR = '/etc/cron.d'
RUNLEVELS = ('sysinit', 'boot', 'default')
REFUSED_INITS = ('runit', 'runsvdir', 's6-svscan')
ROUTES = {'openrc': 'OpenRC local.d', 'cron': 'cron @reboot'}
# the way into the kit's tmux server that never starts one (attach needs a running server) and
# never works in the pm-kit-anchor session's own window (a new window beside HOLD)
ATTACH = 'tmux -L pm-kit attach -t pm-kit-anchor \\; new-window'
ANCHOR_CG = CGROUP + '/anchor'
TMUX_E = (3, 2)            # the launcher's windows there pass their variables with new-session -e
UNDO_HOOK = 'sudo rm -f %s %s %s' % (LOCALD, CROND, SCRIPT)
UNDO_LOCAL = 'sudo rc-update del local default'
LOCALD_TEXT = ('#!/bin/sh\n'
               '# pm-kit: written by tools/machine_setup.py --delegation; OpenRC runs it at boot.\n'
               '# Undo: ' + UNDO_HOOK + '\n'
               'exec ' + SCRIPT + '\n')
CROND_TEXT = ('# pm-kit: written by tools/machine_setup.py --delegation; cron runs it at boot.\n'
              '# Undo: ' + UNDO_HOOK + '\n'
              '@reboot root ' + SCRIPT + '\n')
BINARIES = ('mkdir', 'chown', 'python3', 'tmux')
SHELL = '/bin/sh'
SCRIPT_VARS = ('cg', 'g', 'c', 'f', 'd', 'files')
RUN_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
_PLAIN = re.compile(r'/[A-Za-z0-9_.+@/-]*\Z')

# The pm-kit-anchor session's one window: a python3 that waits for ever, deaf to Ctrl-C, so the
# session holds the server and holds no shell for an `exit` to end; root sees it in /proc
# (anchor_down). The user works in windows and sessions of their own beside it (ATTACH).
HOLD = ('import signal\n'
        'signal.signal(signal.SIGINT,signal.SIG_IGN)\n'
        'while True:\n'
        '    signal.pause()\n')
HOLD_ARGS = ('-I', '-S', '-c', HOLD)

# The drop to the kit user and the tmux server's start (python -I -S -c, inside the script's
# single quotes, so it holds no quote of that kind and no $): argv uid, gid, home, tmux, python3.
# Each id is read back and root seen gone before tmux runs, with HOME and PATH alone. A server
# already answering on the kit's socket is used only when it runs in the anchor (else refused:
# one started from another shell is not contained); its pm-kit-anchor session is made when
# missing, holding HOLD, and the server is told to outlive its last session (exit-empty off).
DROP = ('import os,socket,subprocess,sys\n'
        'u,g,h,t,p=int(sys.argv[1]),int(sys.argv[2]),sys.argv[3],sys.argv[4],sys.argv[5]\n'
        'os.setgroups([])\n'
        'os.setresgid(g,g,g)\n'
        'os.setresuid(u,u,u)\n'
        'if os.getresuid()!=(u,u,u) or os.getresgid()!=(g,g,g) or os.getgroups() not in ([],[g]):\n'
        '    sys.exit("pm-kit: the drop to the kit user did not hold")\n'
        'try:\n'
        '    os.setuid(0)\n'
        'except OSError:\n'
        '    pass\n'
        'else:\n'
        '    sys.exit("pm-kit: root could be taken back after the drop")\n'
        'try:\n'
        '    os.chdir(h)\n'
        'except OSError:\n'
        '    os.chdir("/")\n'
        'e={"HOME":h,"PATH":"/usr/bin:/bin"}\n'
        'a=[t,"-L","pm-kit"]\n'
        'k=socket.socket(socket.AF_UNIX)\n'
        'try:\n'
        '    k.connect("/tmp/tmux-%d/pm-kit"%u)\n'
        '    up=True\n'
        'except OSError:\n'
        '    up=False\n'
        'k.close()\n'
        'def inside():\n'
        '    try:\n'
        '        ps=open("/sys/fs/cgroup/pm-kit/anchor/cgroup.procs").read().split()\n'
        '    except OSError:\n'
        '        return False\n'
        '    for q in ps:\n'
        '        try:\n'
        '            x=os.readlink("/proc/"+q+"/exe")\n'
        '        except OSError:\n'
        '            continue\n'
        '        if x.split(" (deleted)")[0]==os.path.realpath(t):\n'
        '            return True\n'
        '    return False\n'
        'if up and not inside():\n'
        '    sys.exit("pm-kit: a tmux -L pm-kit server runs outside the kit cgroup, so it is not"\n'
        '             " contained: end it (tmux -L pm-kit kill-server), then run this again")\n'
        'if up and subprocess.run(a+["has-session","-t","pm-kit-anchor"],env=e,'
        'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL'
        ').returncode==0:\n'
        '    sys.exit(0)\n'
        'os.execve(t,a+["new-session","-d","-s","pm-kit-anchor",p,'
        + ','.join('"%s"' % x.replace('\n', '\\n') for x in HOLD_ARGS) +
        ',";","set-option","-s","exit-empty","off"],e)\n')

# SCRIPT's text (boot_script fills it). The header holds no $: every $ below is the script's own.
BOOT = r'''#!/bin/sh
# pm-kit boot hook: written by tools/machine_setup.py --delegation (run as root). It makes the
# cgroup pm-kit with the leaf pm-kit/anchor, gives them to uid {uid}, and starts the kit's tmux
# server (tmux -L pm-kit) in the anchor as that user. It reads no environment and runs no kit file.
# Undo: {undo}
PATH=/usr/bin:/bin
cg=/sys/fs/cgroup
g=$cg/pm-kit
[ -f "$cg/cgroup.controllers" ] || exit 0
{mkdir} -p "$g/anchor" || exit 1
for c in cpuset cpu memory pids; do
  echo "+$c" >"$cg/cgroup.subtree_control" 2>/dev/null
  echo "+$c" >"$g/cgroup.subtree_control" 2>/dev/null
done
files="cgroup.procs cgroup.threads cgroup.subtree_control"
if [ -r /sys/kernel/cgroup/delegate ]; then
  files=
  while IFS= read -r f; do
    case $f in *[!a-z0-9._]*|'') ;; *) files="$files $f";; esac
  done </sys/kernel/cgroup/delegate
fi
for d in "$g" "$g/anchor"; do
  {chown} {uid}:{gid} "$d" || exit 1
  for f in $files; do
    [ -e "$d/$f" ] && {{ {chown} {uid}:{gid} "$d/$f" || exit 1; }}
  done
done
( echo 0 >"$g/anchor/cgroup.procs" || exit 1
  exec {python} -I -S -c '{drop}' {uid} {gid} '{home}' {tmux} {python} </dev/null )
'''


def _id(value, what):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError('the kit user\'s %s %r is not a plain id above 0 (root\'s is refused)'
                         % (what, value))
    return value


def boot_script(uid, gid, home, tools):
    """SCRIPT's text for the kit user (uid, gid, home), running the binaries `tools` names
    ({name: absolute path} for BINARIES). ValueError for an id that is not an int above 0, or a
    home or a path that is not absolute and plain (each is written into sh, bare or in '...')."""
    _id(uid, 'uid')
    _id(gid, 'gid')
    if not isinstance(home, str) or not _PLAIN.match(home):
        raise ValueError('the kit user\'s home %r is not an absolute path of plain characters'
                         % (home,))
    paths = {}
    for name in BINARIES:
        p = tools.get(name)
        if not isinstance(p, str) or not _PLAIN.match(p):
            raise ValueError('%s: %r is not an absolute path of plain characters' % (name, p))
        paths[name] = p
    if "'" in DROP or '$' in DROP:
        raise ValueError('DROP holds a single quote or a $')
    return BOOT.format(uid=uid, gid=gid, home=home, undo=UNDO_HOOK, drop=DROP,
                       mkdir=paths['mkdir'], chown=paths['chown'], python=paths['python3'],
                       tmux=paths['tmux'])


def boot_route(box):
    """('openrc' or 'cron', None): the boot hook's route on this box with no systemd; or
    (None, why not)."""
    lower = '; containment stays at the lower tier, shown as such'
    if not box.cgroup2():
        return None, 'no cgroup v2 mount at /sys/fs/cgroup, which the boot hook needs' + lower
    init = box.init()
    if init in REFUSED_INITS:
        return None, ('PID 1 is %s: the boot hook is set up for OpenRC or a cron @reboot line only, '
                      'so a %s box is refused' % (init, init)) + lower
    if box.exists(OPENRC_RUN) and box.exists(LOCALD_DIR):
        return 'openrc', None
    if box.exists(CROND_DIR):
        return 'cron', None
    return None, ('no systemd, no OpenRC local.d and no %s on this box: no boot hook can be set up'
                  % CROND_DIR) + lower


class Host(object):
    """The root side, live: the file writes and checks, the daemon reload and the runs (the boot
    script, rc-update). Tests pass their own."""

    def write(self, path, text, mode=0o644):
        """Write `text` to `path` (`mode`; root's when run as root) through a temp file made new
        (O_EXCL, no link followed) and os.replace, its folder made (mode 0755, whatever root's
        umask)."""
        make_folder(os.path.dirname(path))
        tmp = path + '.tmp'
        if os.path.lexists(tmp):
            os.unlink(tmp)
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0),
                     mode)
        with os.fdopen(fd, 'w', newline='\n') as f:
            f.write(text)
        os.chmod(tmp, mode)
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            os.chown(tmp, 0, 0)
        os.replace(tmp, path)

    def read(self, path):
        try:
            with open(path) as f:
                return f.read()
        except FileNotFoundError:
            return None

    def lstat(self, path):
        """os.lstat(path), None when there is nothing there."""
        try:
            return os.lstat(path)
        except FileNotFoundError:
            return None

    def stat(self, path):
        """os.stat(path), None when there is nothing there."""
        try:
            return os.stat(path)
        except FileNotFoundError:
            return None

    def realpath(self, path):
        return os.path.realpath(path)

    def run(self, argv):
        """Run argv (the boot script, rc-update) with PATH alone; RuntimeError when it fails."""
        r = spawn.run(list(argv), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                      timeout=120, env={'PATH': RUN_PATH})
        if r.returncode != 0:
            raise RuntimeError('%s exit %d: %s' % (' '.join(argv), r.returncode,
                                                   (r.stderr or r.stdout or '').strip()))

    def reload(self):
        r = spawn.run(RELOAD, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError('%s exit %d: %s' % (' '.join(RELOAD), r.returncode,
                                                   (r.stderr or r.stdout).strip()))


class DelegationContext(object):
    """What the mode reads: the kit, `box` (linuxbox.Box), `host` (Host) and `root`, the folder the
    absolute paths are under (/ live; a test's folder)."""

    def __init__(self, kit, box=None, host=None, root='/'):
        self.kit = os.path.abspath(kit)
        self.box = box or linuxbox.Box()
        self.host = host or Host()
        self.root = root

    def path(self, p):
        return os.path.join(self.root, *p.lstrip('/').split('/'))


def survey(ctx, apply=False):
    plan = Plan((SECTION,))
    if platform.NAME != 'linux':
        plan.refuse('--delegation is a Linux step (this is %s)' % platform.NAME)
        return plan
    box = ctx.box
    release = box.osrelease()
    if linuxbox.wsl(release):
        plan.refuse('this is WSL (the kernel release %s names Microsoft): the kit does not run in WSL yet'
                    % (release or '').strip())
        return plan
    route = None
    if not box.systemd():
        route, why = boot_route(box)
        if route is None:
            plan.refuse(why)
            return plan
    owned = (DROPIN, linuxbox.MARKER) if route is None else (
        SCRIPT, LOCALD if route == 'openrc' else CROND, linuxbox.MARKER)
    me = box.uid()
    if me == 0:
        user = box.sudo_user()
        if user is None or user[0] == 0:
            plan.refuse('run it through sudo from the kit user\'s account (no SUDO_UID and SUDO_USER, or '
                        'they name root), so the marker names the kit user')
            return plan
    else:
        user = (me, box.name(me))
        if apply:
            plan.refuse('not root: %s and %s are root-owned; run: %s'
                        % (', '.join(owned[:-1]), owned[-1], linuxbox.delegation_command(ctx.kit)))
            return plan
        plan.note(SECTION, 'not root: --apply is refused here; run: %s'
                  % linuxbox.delegation_command(ctx.kit))
    hook = None
    if route is not None:
        hook = _hook_checks(plan, ctx, route, user)
        if hook is None:
            return plan
    marker = ctx.path(linuxbox.MARKER)
    try:
        had = ctx.host.read(marker)
    except OSError as e:
        plan.refuse('cannot read %s: %s' % (linuxbox.MARKER, e))
        return plan
    if had is not None:
        try:
            uid, name = linuxbox.read_marker(had)
        except ValueError as e:
            plan.refuse(str(e))
            return plan
        if uid != user[0]:
            plan.refuse('this box\'s kit user is %s (uid %d, %s): one kit user per box, so %s is refused'
                        % (name, uid, linuxbox.MARKER, user[1]))
            return plan
    if not (_dropin_changes(plan, ctx) if hook is None else _hook_changes(plan, ctx, route, hook)):
        return plan
    want = linuxbox.marker_text(*user)
    if had != want:
        plan.add(SECTION, 'write %s: %s (uid %d) is the box\'s one kit user; machine setup refuses any other'
                 % (linuxbox.MARKER, user[1], user[0]),
                 lambda: ctx.host.write(marker, want),
                 undo='sudo rm %s' % linuxbox.MARKER)
    return plan


def _dropin_changes(plan, ctx):
    """The systemd drop-in and its reload, planned when the drop-in differs; False: refused."""
    dropin = ctx.path(DROPIN)
    try:
        have = ctx.host.read(dropin)
    except OSError as e:
        plan.refuse('cannot read %s: %s' % (DROPIN, e))
        return False
    if have != DROPIN_TEXT:
        plan.add(SECTION, '%s %s: Delegate=cpuset cpu io memory pids for every user\'s systemd manager'
                 % ('write' if have is None else 'rewrite', DROPIN),
                 lambda: ctx.host.write(dropin, DROPIN_TEXT),
                 undo=('sudo rm %s && sudo systemctl daemon-reload' % DROPIN) if have is None
                 else 'its old text was: %r' % have)
        plan.add(SECTION, 'systemctl daemon-reload (systemd reads the drop-in)', ctx.host.reload)
        plan.note(SECTION, 'sign out and in again (or reboot) once: the user manager takes the delegation '
                           'when it starts again; then `kit machine_lock status` shows the tier')
    return True


def _alone(st):
    """Why a stat result is not root's alone (owned by root, written by no one else), or None."""
    if st.st_uid != 0:
        return 'is not owned by root (uid %d)' % st.st_uid
    if statmod.S_IMODE(st.st_mode) & 0o022:
        return 'is writable by group or others (mode %04o)' % statmod.S_IMODE(st.st_mode)
    return None


def _folders(path):
    """The folders above the absolute `path`, nearest first, / last."""
    out, p = [], path
    while True:
        up = os.path.dirname(p)
        if up == p:
            return out
        out.append(up)
        p = up


def _binary(host, name, path):
    """Why the binary `path` (for `name`) may not be run as root at boot, or None: it, its real
    path and every folder above each must be root's alone."""
    if not path:
        return 'no %s on PATH: the boot script runs it' % name
    if not _PLAIN.match(path):
        return '%s: %r is not an absolute path of plain characters' % (name, path)
    real = host.realpath(path)
    for p in [path, real] + _folders(path) + _folders(real):
        st = host.stat(p)
        if st is None:
            return '%s (for %s) is not there' % (p, name)
        folder = p not in (path, real)
        kind = statmod.S_ISDIR(st.st_mode) if folder else statmod.S_ISREG(st.st_mode)
        if not kind:
            return '%s (for %s) is not a %s' % (p, name, 'folder' if folder else 'plain file')
        why = _alone(st)
        if why:
            return '%s (for %s) %s: the boot script runs it as root' % (p, name, why)
    return None


def _hook_checks(plan, ctx, route, user):
    """The boot hook's facts, checked: {text, entry, rc}, or None (refused, said in plan)."""
    box, host = ctx.box, ctx.host
    acct = box.account(user[0])
    if acct is None:
        plan.refuse('no account for uid %d in the password database: the boot script needs its gid '
                    'and home' % user[0])
        return None
    bins = {n: box.which(n) for n in BINARIES}
    needs = [(n, bins[n]) for n in BINARIES] + [('sh', SHELL)]
    local = route == 'openrc' and not any(
        box.exists('/etc/runlevels/%s/local' % lvl) for lvl in RUNLEVELS)
    rc = box.which('rc-update') if local else None
    if local:
        needs.append(('rc-update', rc))
    for name, path in needs:
        why = _binary(host, name, path)
        if why:
            plan.refuse(why)
            return None
    why = _old_tmux(box.tmux_version(bins['tmux']), bins['tmux'])
    if why:
        plan.refuse(why)
        return None
    try:
        text = boot_script(user[0], acct[0], acct[1], bins)
    except ValueError as e:
        plan.refuse(str(e))
        return None
    entry = LOCALD if route == 'openrc' else CROND
    for p in ('/etc', os.path.dirname(SCRIPT), os.path.dirname(entry), SCRIPT, entry,
              linuxbox.MARKER):
        st = host.lstat(ctx.path(p))
        if st is None:
            continue
        why = 'is a link' if statmod.S_ISLNK(st.st_mode) else _alone(st)
        if why:
            plan.refuse('%s %s: the boot hook writes only where root alone can, and through no link'
                        % (p, why))
            return None
    return {'text': text, 'entry': entry, 'rc': rc, 'bins': bins}


def _old_tmux(said, path):
    """Why the tmux at `path` (`said`: its `tmux -V`, None when it gave none) is too old for the
    boot hook, or None: the launcher's windows in the kit's server pass their variables with
    new-session -e, which tmux 3.2 added."""
    from kitlib.launcher import linuxwin
    have = linuxwin.tmux_version(said)
    if have is not None and have >= TMUX_E:
        return None
    return ('%s is %s: the boot hook needs tmux %d.%d or later (the launcher opens its windows in '
            'the kit\'s tmux server with new-session -e, which 3.2 added); install a newer tmux, or '
            'decline this step and stay at the lower tier'
            % (path, (said or '').strip() or 'of a version `tmux -V` does not name',
               TMUX_E[0], TMUX_E[1]))


def anchor_down(box, host, bins):
    """Why the boot script must run again for the kit's tmux server, or None when it runs in the
    anchor with its pm-kit-anchor session: the anchor gone or empty (cgroup.events populated 0),
    no tmux server in it, or no HOLD process (that session's window) in it. Read by root from
    /sys and /proc; the script starts only what is missing."""
    events = box.read(ANCHOR_CG + '/cgroup.events')
    if events is None:
        return 'the kit\'s cgroup %s is not there' % ANCHOR_CG
    if 'populated 1' not in events.splitlines():
        return '%s is empty (cgroup.events: populated 0): the kit\'s tmux server has ended' % ANCHOR_CG
    tmux = {bins['tmux'], host.realpath(bins['tmux'])}
    hold = [bins['python3']] + list(HOLD_ARGS)
    server = held = False
    for pid in (box.read(ANCHOR_CG + '/cgroup.procs') or '').split():
        exe = box.exe(pid)
        if exe is not None and exe.split(' (deleted)')[0] in tmux:
            server = True
        cmd = box.read('/proc/%s/cmdline' % pid)
        if cmd is not None and cmd.split('\0')[:-1] == hold:
            held = True
    if not server:
        return 'no tmux server runs in %s: the kit\'s tmux server has ended' % ANCHOR_CG
    if not held:
        return 'the kit\'s tmux server has no pm-kit-anchor session holding it'
    return None


def _hook_changes(plan, ctx, route, hook):
    """The boot hook's writes, rc-update and the run, each planned when needed; False: refused."""
    host = ctx.host
    script, entry = ctx.path(SCRIPT), ctx.path(hook['entry'])
    etext, emode = (LOCALD_TEXT, 0o755) if route == 'openrc' else (CROND_TEXT, 0o644)
    try:
        had_script, had_entry = host.read(script), host.read(entry)
    except OSError as e:
        plan.refuse('cannot read the boot hook\'s files: %s' % e)
        return False
    wrote = False
    if had_script != hook['text']:
        wrote = True
        plan.add(SECTION, '%s %s (0755): the boot script, which makes the kit\'s cgroup %s, gives it '
                          'to the kit user and starts the kit\'s tmux server in it'
                 % ('write' if had_script is None else 'rewrite', SCRIPT, CGROUP),
                 lambda: host.write(script, hook['text'], 0o755),
                 undo=('sudo rm -f %s' % SCRIPT) if had_script is None
                 else 'its old text was: %r' % had_script)
    if had_entry != etext:
        wrote = True
        plan.add(SECTION, '%s %s (%04o): %s runs the boot script at boot'
                 % ('write' if had_entry is None else 'rewrite', hook['entry'], emode,
                    'OpenRC\'s local service' if route == 'openrc' else 'cron'),
                 lambda: host.write(entry, etext, emode),
                 undo=('sudo rm -f %s' % hook['entry']) if had_entry is None
                 else 'its old text was: %r' % had_entry)
    if hook['rc']:
        argv = [hook['rc'], 'add', 'local', 'default']
        plan.add(SECTION, 'rc-update add local default: OpenRC runs /etc/local.d at boot',
                 lambda: host.run(argv), undo=UNDO_LOCAL)
    if wrote or hook['rc']:
        plan.add(SECTION, 'run %s once now: the cgroup and the kit\'s tmux server, so no reboot is '
                          'needed' % SCRIPT, lambda: host.run([script]))
    else:
        down = anchor_down(ctx.box, host, hook['bins'])
        if down:
            plan.add(SECTION, 'run %s again: %s; it starts only what is missing, so no reboot is '
                              'needed' % (SCRIPT, down), lambda: host.run([script]))
    plan.note(SECTION, 'containment is strong for work started from the kit\'s tmux server (%s; the '
                       'launcher opens its windows there); work started from any other shell stays at '
                       'the lower tier, and says why' % ATTACH)
    plan.note(SECTION, 'the kit\'s tmux server outlives its sessions; if it is ended (kill-server), '
                       'run this step again with sudo, or reboot: a `tmux -L pm-kit` started from a '
                       'shell is not contained, and this step refuses to use one')
    if route == 'cron':
        plan.note(SECTION, 'the cron daemon must start at boot for the @reboot line to run')
    plan.note(SECTION, 'undo: %s%s; the cgroup goes at the next reboot'
              % (UNDO_HOOK, (', then ' + UNDO_LOCAL) if hook['rc'] else ''))
    return True
