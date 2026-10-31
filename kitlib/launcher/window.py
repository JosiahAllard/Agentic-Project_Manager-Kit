"""The window a launched session runs in (docs/launcher.md "The window").

`script()` builds the PowerShell the window runs, in this order:
  1. the window title (the session's name: stop.py finds the window by it);
  2. every user environment variable read fresh from the registry, but Path, TEMP and TMP (the
     launching process may predate the owner's latest ones);
  3. every inherited CLAUDE*/ANTHROPIC* variable cleared (a window started from a Claude session
     inherits its child-session marker, which turns transcripts off, and its auth routing), then
     CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1;
  4. PM_KIT_ROLE, the session mark (kitlib.sessionmark; role_for), and PM_KIT_SESSION_NAME;
  5. CLAUDE_CONFIG_DIR for the account (removed for the default folder);
  6. AGENT_TEMP set again, and TEMP and TMP pointed at it, when the launcher sees one (a session
     whose guard allows scratch writes only there finds all three; the PM resume too);
  7. the start folder (a home worktree with --home-worktree: home.py), then
     `claude ... [--add-dir '<main checkout>'] --permission-mode auto --remote-control '<name>' -n '<name>' '<prompt>'`
     (a resume: `claude --resume '<id>' [--fork-session] [--agent ...] --remote-control ...`).
Every value is single-quoted with quotes doubled. `open_window` starts it minimized and unfocused:
`conhost.exe powershell.exe -NoExit -EncodedCommand <base64 UTF-16LE>` through WScript.Shell.Run
with show flag 7 (SW_SHOWMINNOACTIVE; conhost honours it), so quotes in a prompt survive.

On Linux (kitlib.platform.NAME 'linux', read at each call) q, script and open_window are
kitlib.launcher.linuxwin's: bash in a tmux session, or in launcher.terminal's command. shown,
opened_in and open_args give the platform's words for the launcher's lines and open_window's
arguments.
"""
import base64
import re
import subprocess

from kitlib import agentname, platform, sessionmark

ROLE_ENV = sessionmark.ENV
NAME_ENV = 'PM_KIT_SESSION_NAME'
# The session kinds and the mark each sets (docs/guard.md "Session mark").
COORDINATOR = 'coordinator'
DEEP_WORKER = 'deep-worker'
CONSULTANT = 'consultant'
PM = 'pm'
PLAIN = 'plain'
_BY_AGENT = {'coordinator': COORDINATOR, 'deep-worker': DEEP_WORKER, 'consultant': CONSULTANT}
MODEL = re.compile(r'^[A-Za-z0-9._-]+(\[1m\])?$')
# an agent name, bare or with the kit plugin's prefix (kitlib.agentname); no other colon
AGENT = re.compile(r'^(?:%s)?[A-Za-z0-9_-]+$' % re.escape(agentname.PREFIX))
# claude's permission modes (the list optional/bridge/hosts/claude/terminal.py MODES also holds)
PERMISSION_MODES = ('default', 'acceptEdits', 'auto', 'dontAsk', 'bypassPermissions')
EFFORTS =('low', 'medium', 'high', 'xhigh', 'max')
SHOW_MIN_NO_ACTIVE = 7

USER_ENV = ("foreach ($k in [Environment]::GetEnvironmentVariables('User').Keys) { if (@('Path','TEMP','TMP') "
            "-notcontains $k) { Set-Item -Path ('env:' + $k) -Value ([Environment]::GetEnvironmentVariable($k, 'User')) } }")
# The host's variables, named once for the Windows and the Linux window (linuxwin): the stems a
# window clears (CLAUDECODE too), the session-persistence switch and the account's config folder.
ENV_STEMS = 'CLAUDE|ANTHROPIC'
PERSIST_ENV = 'CLAUDE_CODE_FORCE_SESSION_PERSISTENCE'
CONFIG_ENV = 'CLAUDE_CONFIG_DIR'
CLEAR_CLAUDE = ("Get-ChildItem Env: | Where-Object { $_.Name -match '^(%s)' } | ForEach-Object "
                "{ [Environment]::SetEnvironmentVariable($_.Name, $null, 'Process') }") % ENV_STEMS
PERSIST = "$env:%s = '1'" % PERSIST_ENV


def role_for(agent, coordinator_agent='coordinator'):
    """The PM_KIT_ROLE a session running as `agent` gets: coordinator for the coordinator agent,
    deep-worker, consultant; `plain` for a session with no agent;
    any other agent's own name. Never empty: an unmarked session would count as the owner's. Both
    names are compared without the kit plugin's prefix, and the mark never carries it."""
    if not agent:
        return PLAIN
    name = agentname.bare(agent) or agent       # the prefix alone (AGENT refuses it) is not emptied
    if name == agentname.bare(coordinator_agent):
        return COORDINATOR
    return _BY_AGENT.get(name, name)


def _linux():
    return platform.NAME == 'linux'


def q(text):
    """A PowerShell single-quoted string (on Linux a bash one: linuxwin.q)."""
    if _linux():
        from kitlib.launcher import linuxwin
        return linuxwin.q(text)
    return "'" + str(text).replace("'", "''") + "'"


def session_command(name, prompt, agent=None, model=None, effort=None, autocompact=None, resume=None,
                    add_dir=None, remote_control=True, fork=False, permission_mode=None):
    """`permission_mode` None: today's mode for the session (`auto` for a fresh start, none for a
    resume); a value from PERMISSION_MODES replaces it (ValueError otherwise). `add_dir`: a folder the session's file tools may also use (`--add-dir`; a home worktree's
    main checkout, docs/launcher.md "The home worktree"). `remote_control` False drops only the
    `--remote-control <name>` pair (a measurement; lookup finds the session by `-n`). `fork` with
    `resume`: `--fork-session`, the resumed conversation under a new session id (pm-relaunch)."""
    if permission_mode is not None and permission_mode not in PERMISSION_MODES:
        raise ValueError('unknown permission mode %r (known: %s)'
                         % (permission_mode, ', '.join(PERMISSION_MODES)))
    parts = ['claude']
    if resume:
        parts += ['--resume', q(resume)]
        parts += ['--fork-session'] if fork else []
    if agent:
        parts += ['--agent', agent]
    if model:
        parts += ['--model', q(model)]
    if effort:
        parts += ['--effort', effort]
    if autocompact:
        parts += ['--autocompact', str(int(autocompact))]
    if add_dir:
        parts += ['--add-dir', q(add_dir)]
    if permission_mode is not None:
        parts += ['--permission-mode', permission_mode]
    elif not resume:
        parts += ['--permission-mode', 'auto']
    parts += ['--remote-control', q(name)] if remote_control else []
    parts += ['-n', q(name), q(prompt)]
    return ' '.join(parts)

claude_command = session_command     # the older name, kept for its callers


def script(name, role, folder, command, config_dir=None, agent_temp=None):
    """The window's PowerShell, one statement per step, joined by '; '. config_dir None: the
    default folder (CLAUDE_CONFIG_DIR removed)."""
    if not role:
        raise ValueError('every launched session carries a PM_KIT_ROLE mark')
    if _linux():
        from kitlib.launcher import linuxwin
        return linuxwin.script(name, role, folder, command, config_dir, agent_temp)
    lines = ['$Host.UI.RawUI.WindowTitle = %s' % q(name), USER_ENV, CLEAR_CLAUDE, PERSIST,
             '$env:%s = %s' % (ROLE_ENV, q(role)), '$env:%s = %s' % (NAME_ENV, q(name))]
    lines.append('$env:%s = %s' % (CONFIG_ENV, q(config_dir)) if config_dir
                 else 'Remove-Item Env:%s -ErrorAction SilentlyContinue' % CONFIG_ENV)
    if agent_temp:
        lines += ['if (-not (Test-Path %s)) { New-Item -ItemType Directory -Force %s | Out-Null }'
                  % (q(agent_temp), q(agent_temp)),
                  '$env:AGENT_TEMP = %s' % q(agent_temp),
                  '$env:TEMP = %s' % q(agent_temp), '$env:TMP = %s' % q(agent_temp)]
    lines += ['Set-Location %s' % q(folder), command]
    return '; '.join(lines)


def encode(text):
    return base64.b64encode(text.encode('utf-16-le')).decode('ascii')


def decode(b64):
    """The script of an -EncodedCommand argument, or None."""
    try:
        return base64.b64decode(b64, validate=True).decode('utf-16-le')
    except (ValueError, UnicodeDecodeError):
        return None


def window_command(text):
    return 'conhost.exe powershell.exe -NoExit -EncodedCommand %s' % encode(text)


def runner_script(text):
    """The PowerShell that opens the window (the COM call keeps the show flag honoured)."""
    return '$null = (New-Object -ComObject WScript.Shell).Run(%s, %d, $false)' % (
        q(window_command(text)), SHOW_MIN_NO_ACTIVE)


def open_window(text, run=subprocess.run, name=None, terminal=None):
    """Start the window. RuntimeError when PowerShell reports a failure. On Linux
    linuxwin.open_window(text, name, terminal)."""
    if _linux():
        from kitlib.launcher import linuxwin
        return linuxwin.open_window(text, name, terminal)
    r = run(['powershell', '-NoProfile', '-NonInteractive', '-Command', runner_script(text)],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if r.returncode != 0:
        raise RuntimeError('opening the window failed (powershell exit %d): %s'
                           % (r.returncode, (r.stderr or '').strip()[:300]))


def shown(text):
    """The --what-if line that shows the window's command."""
    if _linux():
        from kitlib.launcher import linuxwin
        return '  %s -c %s' % (linuxwin.SHELL, linuxwin.q(text))
    return '  powershell -NoExit -Command "%s"' % text


def check_name(name, derived=False):
    """LaunchError on Linux for a session name holding whitespace, a quote or a backslash
    (linuxwin.check_name); derived:
    the name was made from the folder's name. Nothing on Windows."""
    if _linux():
        from kitlib.launcher import linuxwin
        linuxwin.check_name(name, derived)


def opened_in(name, terminal=None):
    """Where a started window opened, for the "Started ..." line."""
    if _linux():
        from kitlib.launcher import linuxwin
        return linuxwin.opened_in(name, terminal)
    return 'a new minimized window'


def open_args(name, settings):
    """open_window's keyword arguments beside the text: none on Windows; on Linux the session name
    (the tmux session's) and launcher.terminal."""
    if _linux():
        return {'name': name, 'terminal': settings.terminal}
    return {}
