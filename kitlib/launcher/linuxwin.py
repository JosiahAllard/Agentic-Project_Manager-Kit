"""The window a launched session runs in on Linux (docs/launcher.md "The window"; docs/
linux-support-plan.md section 8, slice 11). kitlib.launcher.window hands its q, script and
open_window here when kitlib.platform.NAME is 'linux'.

`script()` is bash, one statement per line, the Windows window's steps in its order: the title;
every CLAUDE*/ANTHROPIC* variable unset, then CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1; PM_KIT_ROLE
and PM_KIT_SESSION_NAME; CLAUDE_CONFIG_DIR (unset for the default folder); AGENT_TEMP made and set
again, with TMPDIR, TEMP and TMP pointed at it; `cd -- '<folder>' && claude ...`; then
`exec /bin/bash -i`, so the window stays open after claude ends (PowerShell's -NoExit). There is
no registry to read the user's environment from: the session gets the environment of the tmux
server (or the terminal) plus these exports. Every value is single-quoted, a ' written '\\''.

`open_window` runs it as `/bin/bash -c <script>`: in a new detached tmux session by default,
`tmux new-session -d -s <tmux name> /bin/bash -c <script>` (the arguments passed as a list, so
tmux 3.0 or later runs them without a shell), or, when launcher.terminal is set, as that command
with `/bin/bash -c <script>` appended. On a box with no systemd whose boot hook started the kit's
tmux server (`tmux -L pm-kit`) in its cgroup (anchored: that server's pid is under
contain.BASE), the session opens in that server instead, so the session is contained: `tmux -L
pm-kit new-session -d -e K=V ... -s <tmux name> ...`, the caller's PASS_ENV variables passed with
-e (tmux 3.2 or later), since that server was started with HOME and PATH alone; attach with `tmux
-L pm-kit attach -t <tmux name>`. anchor_check also reads that server's #{version} (else `tmux
-V`): below E_VERSION, or unreadable, it is not used; and when it refuses the new-session (but
for a duplicate name), the session opens in the default server. Either way opened_in (from
_opened, set by open_window) says it is not contained there, and why. The tmux name is the session name with anything but
A-Za-z0-9_- made _; a name tmux already has is refused (LaunchError) and nothing is started or
ended. The launcher never ends a tmux session or a terminal: stop (kitlib.launcher.stop) ends
the claude process and this shell, each by pid and creation time, and tmux closes the pane when
its shell has gone.

`carries(cmd, name)` is stop's test that a process's command line is this window's shell for
`name`: /bin/bash -c with a line `export PM_KIT_SESSION_NAME='<name>'` and a `cd -- ` line whose
claude command has `--remote-control '<name>' ` or ` -n '<name>' `, the name quoted exactly, so
`pm` never matches `pm-session`.
"""
import os
import re
import subprocess

from kitlib import sessionmark
from kitlib.launcher import window
from kitlib.launcher.settings import LaunchError
from kitlib.proc import spawn

SHELL = '/bin/bash'
SHELL_NAME = 'bash'         # the shell's process name (its comm)
TMUX = 'tmux'
ANCHOR = 'pm-kit'           # the kit's tmux server (tmux -L pm-kit), which the boot hook starts
# the caller's variables a window in the kit's server gets (that server has HOME and PATH alone)
PASS_ENV = ('PATH', 'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'USER', 'LOGNAME', 'SHELL',
            'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'TZ')
E_VERSION = (3, 2)          # new-session -e came in tmux 3.2
_opened = {}                # tmux name: (in the kit's server, why not), for opened_in
ROLE_ENV = sessionmark.ENV
NAME_ENV = 'PM_KIT_SESSION_NAME'
# the Windows window's variables (window.ENV_STEMS, PERSIST_ENV, CONFIG_ENV), in bash
CLEAR_ENV = 'for v in %s; do unset "$v"; done' % ' '.join(
    '"${!%s@}"' % stem for stem in window.ENV_STEMS.split('|'))
PERSIST = 'export %s=1' % window.PERSIST_ENV
STAY = 'exec %s -i' % SHELL
_UNSAFE = re.compile(r'[^A-Za-z0-9_-]')


def q(text):
    """A bash single-quoted string."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def tmux_name(name):
    """The tmux session name for a session name: anything but A-Za-z0-9_- made _ (tmux reads . and
    : in a target as window and pane)."""
    return _UNSAFE.sub('_', str(name))


UNQUOTED = '\'"\\'          # characters /proc cannot show unambiguously: it keeps no quoting


def bad_name(name):
    """The refusal for a session name holding whitespace, a quote or a backslash (a %s left for a
    note), or None. /proc joins a process's arguments with spaces and keeps no quoting, so
    `--remote-control my project-a1` reads as `my`: lookup could not find the session and
    `stop --name my` would end it."""
    if any(c.isspace() or c in UNQUOTED for c in str(name or '')):
        return ("On Linux a session name cannot hold whitespace, a quote or a backslash: '%s'%%s "
                "(/proc joins a process's arguments with spaces and keeps no quoting, so lookup and "
                "stop would misread it); pass --name without them." % str(name).replace('%', '%%'))
    return None


def check_name(name, derived=False):
    """LaunchError for a name bad_name refuses; derived: made from the folder's name."""
    why = bad_name(name)
    if why:
        raise LaunchError(why % (" (made from the folder's name)" if derived else ''))


def script(name, role, folder, command, config_dir=None, agent_temp=None):
    """The window's bash, one statement per line. config_dir None: the default folder
    (CLAUDE_CONFIG_DIR unset). LaunchError for a name check_name refuses."""
    if not role:
        raise ValueError('every launched session carries a PM_KIT_ROLE mark')
    check_name(name)
    lines = ["printf '\\033]0;%%s\\007' %s" % q(name), CLEAR_ENV, PERSIST,
             'export %s=%s' % (ROLE_ENV, q(role)), 'export %s=%s' % (NAME_ENV, q(name))]
    lines.append('export %s=%s' % (window.CONFIG_ENV, q(config_dir)) if config_dir
                 else 'unset %s' % window.CONFIG_ENV)
    if agent_temp:
        t = q(agent_temp)
        lines += ['mkdir -p -- %s' % t,
                  'export AGENT_TEMP=%s TMPDIR=%s TEMP=%s TMP=%s' % (t, t, t, t)]
    lines += ['cd -- %s && %s' % (q(folder), command), STAY]
    return '\n'.join(lines)


def argv(text):
    return [SHELL, '-c', text]


def anchor_socket():
    """The path of the kit's tmux server's socket (`tmux -L pm-kit`), None off POSIX."""
    if not hasattr(os, 'getuid'):
        return None
    return os.path.join(os.environ.get('TMUX_TMPDIR') or '/tmp', 'tmux-%d' % os.getuid(), ANCHOR)


def tmux_version(text):
    """(major, minor) from `tmux -V` or #{version} ('tmux 3.1c', 'next-3.4', '3.2'), None when
    the text names no version."""
    m = re.search(r'(\d+)\.(\d+)', text or '')
    return (int(m.group(1)), int(m.group(2))) if m else None


def anchor_check(run=None):
    """(True, None) when the kit's tmux server runs in the boot hook's cgroup (its pid's cgroup is
    under contain.BASE) and is tmux E_VERSION or later, so a window opened there is contained;
    else (False, why): why None when there is no such server to name (no hook, a systemd box, the
    server not started, or one started from elsewhere), the reason when that server is in the
    group but too old for a window's -e."""
    sock = anchor_socket()
    if not sock or not os.path.exists(sock):
        return False, None
    run = run or spawn.run
    try:
        r = run([TMUX, '-L', ANCHOR, 'display-message', '-p', '#{pid} #{version}'],
                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False, None
    if r.returncode != 0:
        return False, None
    out = (r.stdout or '').split()
    try:
        pid = int(out[0])
    except (IndexError, ValueError):
        return False, None
    from kitlib.platform.linux import contain
    try:
        cg = contain.cgroup_of(pid)
    except OSError:
        return False, None
    if not cg or not cg.startswith(contain.BASE + '/'):
        return False, None
    text = out[1] if len(out) > 1 else ''
    if tmux_version(text) is None:            # a server with no #{version}: ask the binary
        try:
            v = run([TMUX, '-V'], capture_output=True, text=True, timeout=10)
            text = (v.stdout or '') if v.returncode == 0 else ''
        except (OSError, subprocess.SubprocessError):
            text = ''
    have = tmux_version(text)
    if have is None or have < E_VERSION:
        return False, ("the kit's tmux server is %s, and a window there needs tmux %d.%d or later "
                       '(new-session -e)' % ('tmux %d.%d' % have if have else 'of a version it '
                                             'does not name', E_VERSION[0], E_VERSION[1]))
    return True, None


def anchored(run=None):
    """True when a window opened now goes to the kit's tmux server (anchor_check)."""
    return anchor_check(run)[0]


def tmux_argv(name, text, in_kit=False, env=None):
    """The tmux command that opens the window: in the kit's server when `in_kit`, with the
    caller's PASS_ENV variables (`-e`, tmux 3.2 or later; that server was started at boot with
    HOME and PATH alone), else in the default server."""
    if not in_kit:
        return [TMUX, 'new-session', '-d', '-s', tmux_name(name)] + argv(text)
    env = os.environ if env is None else env
    out = [TMUX, '-L', ANCHOR, 'new-session', '-d']
    for k in PASS_ENV:
        if env.get(k):
            out += ['-e', '%s=%s' % (k, env[k])]
    return out + ['-s', tmux_name(name)] + argv(text)


def opened_in(name, terminal=None, in_kit=None):
    """Where the window opened, for the launcher's "Started ..." line: what open_window kept for
    this name (taken once), else asked of the kit's tmux server now; in_kit given: that."""
    if terminal:
        return 'a new terminal window (%s)' % terminal[0]
    t = tmux_name(name)
    why = None
    if in_kit is None:
        in_kit, why = _opened.pop(t, None) or anchor_check()
    if in_kit:
        return "tmux session '%s' on the kit's server (tmux -L %s attach -t '%s')" % (t, ANCHOR, t)
    said = "tmux session '%s' (tmux attach -t '%s')" % (t, t)
    if why:
        said += "; not on the kit's tmux server, so not contained there: %s" % why
    return said


def open_window(text, name, terminal=None, run=None, popen=None):
    """Start the window: a detached tmux session named tmux_name(name), or the terminal command.
    In the kit's server when anchor_check says so; when that server refuses it for anything but a
    name it has, in the default server instead, the reason kept for opened_in. LaunchError when
    tmux already has that session name (nothing started, nothing ended); RuntimeError when tmux
    or the terminal cannot start or tmux reports a failure."""
    if terminal:
        popen = popen or spawn.popen
        try:
            popen(list(terminal) + argv(text), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        except OSError as e:
            raise RuntimeError('opening the window failed: the terminal command %s could not start '
                               '(%s)' % (terminal[0], e))
        return
    if not name:
        raise ValueError('a tmux window needs the session name')
    run = run or spawn.run
    in_kit, why = anchor_check(run)
    try:
        r = run(tmux_argv(name, text, in_kit), capture_output=True, text=True, timeout=60)
        err = (r.stderr or '').strip()[:300]
        if in_kit and r.returncode != 0 and 'duplicate session' not in err:
            in_kit, why = False, ("the kit's tmux server refused the window (tmux exit %d: %s)"
                                  % (r.returncode, err))
            r = run(tmux_argv(name, text), capture_output=True, text=True, timeout=60)
    except OSError as e:
        raise RuntimeError('opening the window failed: tmux could not start (%s); install tmux or '
                           'set launcher.terminal' % e)
    if r.returncode != 0:
        err = (r.stderr or '').strip()[:300]
        if 'duplicate session' in err:
            raise LaunchError("tmux already has a session '%s' (the name '%s' made safe for tmux): "
                              'nothing was started, and the launcher never ends a tmux session'
                              % (tmux_name(name), name))
        raise RuntimeError('opening the window failed (tmux exit %d): %s' % (r.returncode, err))
    _opened[tmux_name(name)] = (in_kit, why)


def carries(cmd, name):
    """True when the command line `cmd` is this window's shell for exactly `name`."""
    prefix = SHELL + ' -c '
    if not name or not cmd or not cmd.startswith(prefix):
        return False
    lines = cmd[len(prefix):].split('\n')
    quoted = q(name)
    if 'export %s=%s' % (NAME_ENV, quoted) not in lines:
        return False
    return any(line.startswith('cd -- ') and ((' --remote-control %s ' % quoted) in line
                                              or (' -n %s ' % quoted) in line)
               for line in lines)
