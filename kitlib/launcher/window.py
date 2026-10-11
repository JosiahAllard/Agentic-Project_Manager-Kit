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
  7. the start folder, then `claude ... --permission-mode auto --remote-control '<name>' -n '<name>' '<prompt>'`
     (a resume: `claude --resume '<id>' --remote-control ...`).
Every value is single-quoted with quotes doubled. `open_window` starts it minimized and unfocused:
`conhost.exe powershell.exe -NoExit -EncodedCommand <base64 UTF-16LE>` through WScript.Shell.Run
with show flag 7 (SW_SHOWMINNOACTIVE; conhost honours it), so quotes in a prompt survive.
"""
import base64
import re
import subprocess

from kitlib import agentname, sessionmark

ROLE_ENV = sessionmark.ENV
NAME_ENV = 'PM_KIT_SESSION_NAME'
# The session kinds and the mark each sets (docs/guard.md "Session mark").
COORDINATOR = 'coordinator'
DEEP_WORKER = 'deep-worker'
CONSULTANT = 'consultant'
PM = 'pm'
PLAIN = 'plain'
_BY_AGENT = {'coordinator': COORDINATOR, 'conductor': COORDINATOR,
             'deep-worker': DEEP_WORKER, 'consultant': CONSULTANT}
MODEL = re.compile(r'^[A-Za-z0-9._-]+(\[1m\])?$')
# an agent name, bare or with the kit plugin's prefix (kitlib.agentname); no other colon
AGENT = re.compile(r'^(?:%s)?[A-Za-z0-9_-]+$' % re.escape(agentname.PREFIX))
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
SHOW_MIN_NO_ACTIVE = 7

USER_ENV = ("foreach ($k in [Environment]::GetEnvironmentVariables('User').Keys) { if (@('Path','TEMP','TMP') "
            "-notcontains $k) { Set-Item -Path ('env:' + $k) -Value ([Environment]::GetEnvironmentVariable($k, 'User')) } }")
CLEAR_CLAUDE = ("Get-ChildItem Env: | Where-Object { $_.Name -match '^(CLAUDE|ANTHROPIC)' } | ForEach-Object "
                "{ [Environment]::SetEnvironmentVariable($_.Name, $null, 'Process') }")
PERSIST = "$env:CLAUDE_CODE_FORCE_SESSION_PERSISTENCE = '1'"


def role_for(agent, coordinator_agent='coordinator'):
    """The PM_KIT_ROLE a session running as `agent` gets: coordinator for the coordinator agent
    (and the old `conductor` name), deep-worker, consultant; `plain` for a session with no agent;
    any other agent's own name. Never empty: an unmarked session would count as the owner's. Both
    names are compared without the kit plugin's prefix, and the mark never carries it."""
    if not agent:
        return PLAIN
    name = agentname.bare(agent) or agent       # the prefix alone (AGENT refuses it) is not emptied
    if name == agentname.bare(coordinator_agent):
        return COORDINATOR
    return _BY_AGENT.get(name, name)


def q(text):
    """A PowerShell single-quoted string."""
    return "'" + str(text).replace("'", "''") + "'"


def claude_command(name, prompt, agent=None, model=None, effort=None, autocompact=None, resume=None):
    parts = ['claude']
    if resume:
        parts += ['--resume', q(resume)]
    if agent:
        parts += ['--agent', agent]
    if model:
        parts += ['--model', q(model)]
    if effort:
        parts += ['--effort', effort]
    if autocompact:
        parts += ['--autocompact', str(int(autocompact))]
    parts += ['--permission-mode', 'auto'] if not resume else []
    parts += ['--remote-control', q(name), '-n', q(name), q(prompt)]
    return ' '.join(parts)


def script(name, role, folder, command, config_dir=None, agent_temp=None):
    """The window's PowerShell, one statement per step, joined by '; '. config_dir None: the
    default folder (CLAUDE_CONFIG_DIR removed)."""
    if not role:
        raise ValueError('every launched session carries a PM_KIT_ROLE mark')
    lines = ['$Host.UI.RawUI.WindowTitle = %s' % q(name), USER_ENV, CLEAR_CLAUDE, PERSIST,
             '$env:%s = %s' % (ROLE_ENV, q(role)), '$env:%s = %s' % (NAME_ENV, q(name))]
    lines.append('$env:CLAUDE_CONFIG_DIR = %s' % q(config_dir) if config_dir
                 else 'Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue')
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


def open_window(text, run=subprocess.run):
    """Start the window. RuntimeError when PowerShell reports a failure."""
    r = run(['powershell', '-NoProfile', '-NonInteractive', '-Command', runner_script(text)],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if r.returncode != 0:
        raise RuntimeError('opening the window failed (powershell exit %d): %s'
                           % (r.returncode, (r.stderr or '').strip()[:300]))
