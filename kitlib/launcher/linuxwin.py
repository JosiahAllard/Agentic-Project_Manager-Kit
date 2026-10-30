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
-L pm-kit attach -t <tmux name>`. The tmux name is the session name with anything but
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


def anchored(run=None):
    """True when the kit's tmux server runs and runs in the boot hook's cgroup (its pid's cgroup
    is under contain.BASE), so a window opened there is contained; False otherwise (no hook, a
    systemd box, the server not started, or one started from elsewhere)."""
    sock = anchor_socket()
    if not sock or not os.path.exists(sock):
        return False
    run = run or spawn.run
    try:
        r = run([TMUX, '-L', ANCHOR, 'display-message', '-p', '#{pid}'], capture_output=True,
                text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    try:
        pid = int((r.stdout or '').strip())
    except ValueError:
        return False
    from kitlib.platform.linux import contain
    try:
        cg = contain.cgroup_of(pid)
    except OSError:
        return False
    return bool(cg) and cg.startswith(contain.BASE + '/')


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
    """Where the window opened, for the launcher's "Started ..." line. in_kit None: asked of the
    kit's tmux server now."""
    if terminal:
        return 'a new terminal window (%s)' % terminal[0]
    t = tmux_name(name)
    if in_kit is None:
        in_kit = anchored()
    if in_kit:
        return "tmux session '%s' on the kit's server (tmux -L %s attach -t '%s')" % (t, ANCHOR, t)
    return "tmux session '%s' (tmux attach -t '%s')" % (t, t)


def open_window(text, name, terminal=None, run=None, popen=None):
    """Start the window: a detached tmux session named tmux_name(name), or the terminal command.
    LaunchError when tmux already has that session name (nothing started, nothing ended);
    RuntimeError when tmux or the terminal cannot start or tmux reports a failure."""
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
    try:
        r = run(tmux_argv(name, text, anchored(run)), capture_output=True, text=True, timeout=60)
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
