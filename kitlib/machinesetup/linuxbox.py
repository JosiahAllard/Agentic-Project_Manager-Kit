"""The box checks of machine setup on Linux (docs/linux-support-plan.md section 6 and slice 14;
docs/machine-setup.md "Linux"), and the front door tools/kit.py asks.

  front_door(platform)  None when the kit runs on `platform` (sys.platform's spelling); else the
                        refusal: every platform but Windows and Linux (darwin included), and WSL,
                        whose kernel release names Microsoft (plan question 11: a kit in WSL beside
                        the Windows kit would keep a second lock store for the same CPUs).
  Box                   the box's facts, read live; tests pass a fake with the same methods.
  read_marker(text)     the kit user the machine-wide MARKER names, as (uid, name).
  survey(plan, ctx)     the default mode's Linux checks, run first, in the `box` section: WSL
                        refused; running as root refused (the root steps are --delegation and
                        --pm-repo); a MARKER naming another user refused (plan question 10, one kit
                        user per box); with no MARKER, another user's kit state a warning; a
                        container a warning, never a refusal; no git or tmux a named note; and the
                        containment tier probed (kitlib.platform.linux.contain.probe), with the
                        delegation step offered below S1 (kitlib.machinesetup.delegation): the
                        systemd drop-in, or off systemd the boot hook (OpenRC local.d, else cron
                        @reboot) whose group holds the kit's tmux server (`tmux -L pm-kit`), or why
                        neither can be set up here (delegation.boot_route).

The MARKER is written only by the delegation step, as root. Without it the second-user check is a
warning: it reads only what this user can see (another user's home that cannot be read is
skipped). Nothing here prints or exits.
"""
import os
import shutil

OSRELEASE = '/proc/sys/kernel/osrelease'
INIT_COMM = '/proc/1/comm'
CONTAINER_FILES = ('/.dockerenv', '/run/.containerenv')
MARKER = '/etc/pm-kit/kit-user'
USER_POINTER = ('.config', 'pm-kit', 'pointer')     # kitlib.platform.linux.paths.USER_POINTER
FIRST_UID = 1000                                     # the usual first uid of a login user
SECTION = 'box'
TOOLS = (('git', 'the kit runs git for every repository'),
         ('tmux', 'a launched session opens in a detached tmux session unless launcher.terminal '
                  'names another terminal'))


def wsl(osrelease):
    """Whether the kernel release text `osrelease` is WSL's (it names Microsoft)."""
    return 'microsoft' in (osrelease or '').lower()


def front_door(platform, osrelease=None):
    """None when the kit runs here; else why not. `platform` is sys.platform's spelling;
    `osrelease` the kernel release text (default: read from OSRELEASE on Linux)."""
    if platform == 'win32':
        return None
    if not platform.startswith('linux'):
        return 'the kit runs on Windows and Linux only (this is %s)' % platform
    if osrelease is None:
        osrelease = Box().osrelease()
    if wsl(osrelease):
        return ('the kit does not run in WSL yet (the kernel release %s names Microsoft): run it '
                'on Windows, or on a native Linux box' % (osrelease or '').strip())
    return None


def read_marker(text):
    """(uid, name) from MARKER's text, one line `<uid> <name>`; ValueError when it is not that."""
    parts = (text or '').split()
    if len(parts) != 2 or not parts[0].isdigit():
        raise ValueError('%s is not one line "<uid> <name>": %r' % (MARKER, (text or '')[:80]))
    return int(parts[0]), parts[1]


def marker_text(uid, name):
    return '%d %s\n' % (uid, name)


class Box(object):
    """The box, read live (Linux). Each method answers without raising where it can."""

    def osrelease(self):
        try:
            with open(OSRELEASE) as f:
                return f.read()
        except OSError:
            return None

    def container(self):
        """What says this runs in a container (a marker file, the `container` variable), or None."""
        for p in CONTAINER_FILES:
            if os.path.exists(p):
                return p
        if os.environ.get('container'):
            return 'the container variable (%s)' % os.environ['container']
        return None

    def uid(self):
        return os.geteuid()

    def name(self, uid):
        import pwd
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return str(uid)

    def users(self):
        """[(name, uid, home)] of the password database; [] when it cannot be read."""
        try:
            import pwd
            return [(p.pw_name, p.pw_uid, p.pw_dir) for p in pwd.getpwall()]
        except (ImportError, OSError):
            return []

    def marker(self):
        """(text or None, why it cannot be read or None)."""
        try:
            with open(MARKER) as f:
                return f.read(), None
        except FileNotFoundError:
            return None, None
        except OSError as e:
            return None, str(e)

    def which(self, name):
        return shutil.which(name)

    def systemd(self):
        """Whether systemd is the init system (sd_booted's test)."""
        return os.path.isdir('/run/systemd/system')

    def init(self):
        """The name of PID 1 (/proc/1/comm: systemd, init, runit, s6-svscan...), or None."""
        try:
            with open(INIT_COMM) as f:
                return f.read().strip() or None
        except OSError:
            return None

    def exists(self, path):
        return os.path.lexists(path)

    def cgroup2(self):
        """Whether /sys/fs/cgroup is a cgroup v2 mount."""
        from kitlib.platform.linux import contain
        return contain.cgroup2()

    def account(self, uid):
        """(gid, home) of uid in the password database, or None."""
        try:
            import pwd
            p = pwd.getpwuid(uid)
        except (ImportError, KeyError):
            return None
        return p.pw_gid, p.pw_dir

    def tier(self):
        """The containment tier this user gets (contain.probe, run afresh)."""
        from kitlib.platform.linux import contain
        return contain.probe(fresh=True)

    def sudo_user(self):
        """(uid, name) of the user who ran sudo (SUDO_UID, SUDO_USER), or None."""
        uid, name = os.environ.get('SUDO_UID'), os.environ.get('SUDO_USER')
        if not uid or not uid.isdigit() or not name:
            return None
        return int(uid), name


def kit_state(home):
    """The paths in `home` that show a kit user (the machine lock's store, the user pointer
    file) and exist, as far as this user can see."""
    from kitlib.host import registry
    out = []
    for p in (registry.state_path(home, 'machine-lock'), os.path.join(home, *USER_POINTER)):
        try:
            if os.path.lexists(p):
                out.append(p)
        except (OSError, ValueError):
            pass
    return out


def delegation_command(kit):
    return 'sudo python3 %s --delegation --apply' % os.path.join(kit, 'tools', 'machine_setup.py')


def survey(plan, ctx, box=None):
    """The default mode's Linux checks (module doc). Refusals stop the run before anything else is
    read."""
    box = box or getattr(ctx, 'box', None) or Box()
    release = box.osrelease()
    if wsl(release):
        plan.refuse('this is WSL (the kernel release %s names Microsoft): the kit does not run in WSL '
                    'yet; run it on Windows, or on a native Linux box' % (release or '').strip())
        return
    me = box.uid()
    if me == 0:
        plan.refuse('machine setup runs as the kit user, not as root: run it without sudo (the root '
                    'steps are --delegation and --pm-repo, each run with sudo)')
        return
    text, why = box.marker()
    if why:
        plan.refuse('cannot read %s (%s): it names the box\'s one kit user' % (MARKER, why))
        return
    if text is not None:
        try:
            uid, name = read_marker(text)
        except ValueError as e:
            plan.refuse(str(e))
            return
        if uid != me:
            plan.refuse('this box\'s kit user is %s (uid %d, %s, written by the delegation step): one kit '
                        'user per box, so machine setup refuses a second' % (name, uid, MARKER))
            return
    else:
        for name, uid, home in box.users():
            if uid == me or uid < FIRST_UID or not home:
                continue
            for p in kit_state(home):
                plan.note(SECTION, 'WARNING: user %s (uid %d) has kit state at %s: the kit allows one kit '
                                   'user per box, and a second keeps its own lock store for the same CPUs. '
                                   'The delegation step writes %s, which makes this a refusal'
                          % (name, uid, p, MARKER))
                break
    where = box.container()
    if where:
        plan.note(SECTION, 'WARNING: this runs in a container (%s): the kit is supported there only as the '
                           'only kit on its host, and it never claims strong containment there' % where)
    for tool, why_needed in TOOLS:
        if not box.which(tool):
            plan.note(SECTION, 'no %s on PATH: %s; install it' % (tool, why_needed))
    tier_note(plan, ctx, box)


def tier_note(plan, ctx, box):
    """The probed tier, and below S1 the delegation step offered (or why there is none)."""
    try:
        tier = box.tier()
    except (OSError, RuntimeError, ValueError) as e:
        plan.note(SECTION, 'containment could not be probed (%s)' % e)
        return
    plan.note(SECTION, tier.text)
    if tier.name == 'S1':
        return
    if box.container():
        return
    if not box.systemd():
        from . import delegation
        route, why = delegation.boot_route(box)
        if route is None:
            plan.note(SECTION, 'no root step is offered: %s' % why)
        elif box.exists(delegation.SCRIPT):
            plan.note(SECTION, 'the boot hook is set up (%s): containment is strong only for work '
                               'started from the kit\'s tmux server, so start work there: %s'
                      % (delegation.SCRIPT, delegation.ATTACH))
        else:
            plan.note(SECTION, 'the root step that gives strong containment here, a boot hook (%s), is '
                               'offered and may be declined: %s, then start work from the kit\'s tmux '
                               'server (%s)' % (delegation.ROUTES[route], delegation_command(ctx.kit),
                                                delegation.ATTACH))
        return
    plan.note(SECTION, 'the root step that gives strong containment (S1) is offered and may be declined: '
                       '%s, then sign out and in again' % delegation_command(ctx.kit))
