"""The --delegation mode (docs/linux-support-plan.md sections 2.3 and 6, slice 14;
docs/machine-setup.md "Linux"): the one root step that gives strong containment (S1) on a systemd
box, offered by the default mode below S1 and run by the owner with sudo. Declining it is allowed:
the box is then S2 (or lower), shown as such.

Its changes, each planned only when it differs:
- DROPIN, a drop-in for every user's systemd manager (`user@.service.d`) delegating the cpuset,
  cpu, io, memory and pids controllers (DROPIN_TEXT), then `systemctl daemon-reload`. The user
  manager reads it when it starts again: sign out and in (or reboot) once.
- linuxbox.MARKER, the box's one kit user (`<uid> <name>`): the user who ran sudo. With it, the
  default mode refuses any other user (plan question 10).

Refused: off Linux; in WSL; with no systemd (the plan's boot hook for such a box hands a cgroup the
contain backend has no route for, so no root step is offered there yet); a MARKER naming another
user. --apply needs root, through sudo from the kit user's account (SUDO_UID names the user;
root's own login is refused). Nothing here prints or exits.
"""
import os

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


class Host(object):
    """The root side, live: the file writes and the daemon reload. Tests pass their own."""

    def write(self, path, text):
        """Write `text` to `path` (mode 0644) through a temp file and os.replace, its folder made
        (mode 0755, whatever root's umask)."""
        make_folder(os.path.dirname(path))
        tmp = path + '.tmp'
        with open(tmp, 'w', newline='\n') as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)

    def read(self, path):
        try:
            with open(path) as f:
                return f.read()
        except FileNotFoundError:
            return None

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
    if not box.systemd():
        plan.refuse('no systemd on this box: the delegation step is a systemd drop-in, and there is no '
                    'root step for a box without systemd yet; heavy runs go ahead at the tier the box '
                    'allows, shown as such')
        return plan
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
                        % (DROPIN, linuxbox.MARKER, linuxbox.delegation_command(ctx.kit)))
            return plan
        plan.note(SECTION, 'not root: --apply is refused here; run: %s'
                  % linuxbox.delegation_command(ctx.kit))
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
    dropin = ctx.path(DROPIN)
    try:
        have = ctx.host.read(dropin)
    except OSError as e:
        plan.refuse('cannot read %s: %s' % (DROPIN, e))
        return plan
    if have != DROPIN_TEXT:
        plan.add(SECTION, '%s %s: Delegate=cpuset cpu io memory pids for every user\'s systemd manager'
                 % ('write' if have is None else 'rewrite', DROPIN),
                 lambda: ctx.host.write(dropin, DROPIN_TEXT),
                 undo=('sudo rm %s && sudo systemctl daemon-reload' % DROPIN) if have is None
                 else 'its old text was: %r' % have)
        plan.add(SECTION, 'systemctl daemon-reload (systemd reads the drop-in)', ctx.host.reload)
        plan.note(SECTION, 'sign out and in again (or reboot) once: the user manager takes the delegation '
                           'when it starts again; then `kit machine_lock status` shows the tier')
    want = linuxbox.marker_text(*user)
    if had != want:
        plan.add(SECTION, 'write %s: %s (uid %d) is the box\'s one kit user; machine setup refuses any other'
                 % (linuxbox.MARKER, user[1], user[0]),
                 lambda: ctx.host.write(marker, want),
                 undo='sudo rm %s' % linuxbox.MARKER)
    return plan
