"""Codex's hook payloads read as kitlib.host.Action (plan 2.2; from the Codex branch's actions.py).

Every read of a Codex hook payload's field names is here, and nowhere in kitlib. The payload shapes
are the ones the Codex hooks documentation gives and the branch's tests recorded; no installed
client's payload has been captured yet (hosts/codex/README.md, "Not verified").

  parse(event_name, payload) -> Action, or a tuple of Actions for apply_patch
      SessionStart and PostCompact are session_start, SubagentStart is subagent_start.
      PreToolUse: `tool_name` sorts the call; `cwd` must be an absolute path.
      - apply_patch: its patch text (tool_input.command, or the tool_input string itself) names
        every file it adds, updates, deletes or moves to (patch_targets). One edit Action per
        target, in patch order, `retarget` the same patch line aimed at `{path}`: hookmain judges
        each, and any deny refuses the whole patch. A patch of the wrong shape raises, so no part
        of it is judged as allowed.
      - a file tool (FILE_TOOLS) is one edit, its target from the tool's own key.
      - a shell tool (SHELL_TOOLS) is kind shell: the command (`command`, else `cmd`; a legacy
        argv list `[shell, -lc|-c|-Command, text]` gives its text), its dialect from `shell` (or the
        argv's interpreter), and `workdir` made absolute against cwd when the call names one.
        What the guard cannot fully judge is parsed anyway and carries a `refusal`
        (CodexAction): no named interpreter (judged as PowerShell, Codex's shell on Windows), an
        interpreter it reads only through PowerShell (cmd.exe: its text judged in cmd.exe's dialect, as `cmd /c '<text>'`) or not at
        all (no dialect, the shared shell rules pass it), a
        TTY or interactive call, standard input handed to the command. hosts.codex.adapter.policy
        refuses those in a marked session; the owner's own session is judged as parsed.
      - write_stdin: kind other; text sent to a running process carries a refusal.
      - mcp__*: kind mcp, the full tool name.
      - anything else: kind other, the tool name kept (hosts.codex.adapter.policy holds a marked
        session to its allowlist).
  agent(payload)  always None: Codex's hooks carry no subagent identity (hosts.json
      capabilities; HOOK_PROTECTION in the README). A child's hook payload names its parent's
      session, and a child inherits its parent's PM_KIT_ROLE, so only the role policy applies.

PM_KIT_ROLE is not in the payload: the guard reads it from the hook's own environment.
"""
import dataclasses
import ntpath
import os
import posixpath

from kitlib import platform
from kitlib.host import Action


@dataclasses.dataclass(frozen=True)
class CodexAction(Action):
    """An Action the guard judges as it is, with the reason Codex's policy refuses it in a marked
    session (hosts.codex.adapter.policy): a shape the guard cannot see through."""
    refusal: str = ''

FILE_TOOLS = {'Edit': 'file_path', 'Write': 'file_path', 'MultiEdit': 'file_path',
              'NotebookEdit': 'notebook_path'}
SHELL_TOOLS = ('Bash', 'PowerShell', 'exec_command', 'shell', 'shell_command')
PATCH = 'apply_patch'
STDIN = 'write_stdin'
STDIN_KEYS = {'session_id', 'chars', 'yield_time_ms', 'max_output_tokens'}
MCP = 'mcp__'
KINDS = {'SessionStart': 'session_start', 'PostCompact': 'session_start', 'SubagentStart': 'subagent_start'}
TOOL_EVENT = 'PreToolUse'
POWERSHELLS = {'powershell', 'powershell.exe', 'pwsh', 'pwsh.exe'}
ARGV_FLAGS = ('-lc', '-c', '-command', '-noprofile')
BASHES = {'bash', 'bash.exe', 'sh', 'sh.exe'}
PATCH_HEADERS = ('Add', 'Update', 'Delete')


def default_dialect():
    """Codex runs its shell tool in PowerShell on Windows and in bash elsewhere (read at call time)."""
    return 'powershell' if platform.NAME == 'windows' else 'bash'


def agent(payload):
    return None


def absolute(path):
    """True for an absolute path as Windows or POSIX writes one (the hook may be tested off Windows)."""
    return isinstance(path, str) and bool(path) and (ntpath.isabs(path) or posixpath.isabs(path)
                                                     or bool(ntpath.splitdrive(path)[0]))


def cmd_c(text):
    """cmd.exe `text` as a PowerShell `cmd /c '<text>'` call: one single-quoted literal (each ' doubled)
    that the guard's PowerShell reading passes whole to its cmd.exe dialect (kitlib.guard.commands
    reads `cmd /c` text as sh.CMD), so no PowerShell comment (#, <#), quote or operator in the text
    hides what cmd.exe runs."""
    return "cmd /c '%s'" % text.replace("'", "''")


def is_cmd(value):
    return isinstance(value, str) and value.replace('\\', '/').rsplit('/', 1)[-1].lower() in CMD


def dialect(value):
    """'powershell' or 'bash' for an interpreter name or path, None for one the guard cannot read."""
    if not isinstance(value, str):
        return None
    name = value.replace('\\', '/').rsplit('/', 1)[-1].lower()
    return 'powershell' if name in POWERSHELLS else 'bash' if name in BASHES else None


CMD = {'cmd', 'cmd.exe'}


def shell_kind(tool, data):
    """(dialect or None, refusal): the call's explicit interpreter's dialect; with none named, the
    PowerShell tool's own, else default_dialect() and a refusal; cmd.exe (_shell judges its text in
    cmd.exe's dialect, cmd_c) and one the guard cannot read, with a refusal. Each refusal names
    default_dialect()'s shells: PowerShell or Git Bash on Windows, bash elsewhere."""
    value = data.get('shell')
    if value is None:
        if tool == 'PowerShell':
            return 'powershell', ''
        kind = default_dialect()
        return kind, ('the call names no shell; give an explicit %s shell'
                      % ('PowerShell or Git Bash' if kind == 'powershell' else 'bash'))
    kind = dialect(value)
    if kind is None:
        if default_dialect() == 'powershell':
            why = 'the guard reads the shell %r only through PowerShell; use PowerShell or Git Bash' % (value,)
        else:
            why = 'the guard cannot read the shell %r; use bash' % (value,)
        return ('powershell' if is_cmd(value) else None), why
    return kind, ''


def patch_targets(command):
    """((operation, path), ...) for every file the apply_patch text adds, updates, deletes or
    moves to, in order; `Move` for a rename's new name. A patch of any other shape is a ValueError,
    so an ambiguous patch fails before any of it is judged."""
    if not isinstance(command, str):
        raise ValueError('apply_patch requires a string command')
    lines = command.strip().splitlines()
    if len(lines) < 3 or lines[0] != '*** Begin Patch' or lines[-1] != '*** End Patch':
        raise ValueError('expected one complete apply_patch envelope')
    targets, operation, body, moved = [], None, False, False
    for line in lines[1:-1]:
        header = next((h for h in PATCH_HEADERS if line.startswith('*** %s File: ' % h)), None)
        if header:
            target = line.split(': ', 1)[1]
            if not target.strip() or '\x00' in target:
                raise ValueError('patch target is empty or invalid')
            targets.append((header, target))
            operation, body, moved = header, False, False
        elif line.startswith('*** Move to: '):
            target = line[len('*** Move to: '):]
            if operation != 'Update' or body or moved or not target.strip() or '\x00' in target:
                raise ValueError('invalid patch rename')
            targets.append(('Move', target))
            moved = True
        elif line == '*** End of File' and operation == 'Update':
            body = True
        elif operation == 'Add' and line.startswith('+'):
            body = True
        elif operation == 'Update' and (line.startswith(('+', '-', ' ', '@@')) or line == ''):
            body = True
        else:
            raise ValueError('unsupported or malformed patch directive')
    if not targets:
        raise ValueError('patch has no file actions')
    return tuple(targets)


def _patch_line(operation):
    return 'apply_patch "*** %s: {path}"' % ('Move to' if operation == 'Move' else operation + ' File')


def _argv(command, data):
    """(command text, data) for a legacy argv list: `[shell, flag, text]` names its interpreter;
    any other list is its words joined."""
    if not command or not all(isinstance(w, str) for w in command):
        raise ValueError('a shell argv is a list of strings')
    if len(command) >= 3 and command[-2].lower() in ARGV_FLAGS and dialect(command[0]):
        return command[-1], dict(data, shell=data.get('shell') or command[0])
    return ' '.join(command), data


def _shell(tool, data, cwd):
    if not isinstance(data, dict):
        raise ValueError('shell input must be an object')
    command = data.get('command', data.get('cmd'))
    if isinstance(command, list):
        command, data = _argv(command, data)
    if not isinstance(command, str) or not command.strip():
        raise ValueError('shell command is required')
    workdir = data.get('workdir')
    if workdir is not None:
        if not isinstance(workdir, str) or not workdir:
            raise ValueError('shell workdir must be a path')
        workdir = workdir if absolute(workdir) else os.path.normpath(os.path.join(cwd, workdir))
    kind, refusal = shell_kind(tool, data)
    if is_cmd(data.get('shell')):
        command = cmd_c(command)
    if data.get('tty') not in (None, False) or data.get('interactive') not in (None, False):
        refusal = 'an interactive or TTY command takes input the guard never sees'
    elif any(data.get(key) not in (None, '') for key in ('stdin', 'input', 'chars')):
        refusal = 'input handed to a command is a script the guard never sees'
    fields = dict(tool=tool, command=command, cwd=cwd, shell=kind, workdir=workdir)
    return CodexAction('shell', refusal=refusal, **fields) if refusal else Action('shell', **fields)


def parse(event_name, payload):
    if not isinstance(payload, dict):
        raise ValueError('a %s payload is an object' % event_name)
    named = payload.get('hook_event_name')
    if named is not None and named != event_name:
        raise ValueError('the payload is a %r event, not %s' % (named, event_name))
    cwd = payload.get('cwd')
    if event_name in KINDS:
        return Action(KINDS[event_name], cwd=cwd if isinstance(cwd, str) else None)
    if event_name != TOOL_EVENT:
        raise ValueError('not an event the kit hooks on Codex: %r' % (event_name,))
    tool, data = payload.get('tool_name'), payload.get('tool_input')
    if not isinstance(tool, str) or not tool:
        raise ValueError('tool_name is required')
    if not absolute(cwd):
        raise ValueError('cwd must be absolute')
    if tool == PATCH:
        command = data.get('command', data.get('input')) if isinstance(data, dict) else data
        return tuple(Action('edit', tool=tool, path=path, cwd=cwd, retarget=_patch_line(op))
                     for op, path in patch_targets(command))
    if tool in SHELL_TOOLS:
        return _shell(tool, data, cwd)
    if tool in FILE_TOOLS:
        key = FILE_TOOLS[tool]
        if not isinstance(data, dict) or not isinstance(data.get(key), str) or not data[key]:
            raise ValueError('file edit requires a literal target')
        return Action('edit', tool=tool, path=data[key], cwd=cwd, retarget='%s(%s="{path}")' % (tool, key))
    if tool == STDIN:
        if not isinstance(data, dict) or set(data) - STDIN_KEYS:
            raise ValueError('unrecognized stdin transport input')
        if data.get('chars') not in (None, ''):
            return CodexAction('other', tool=tool, cwd=cwd,
                               refusal='text sent to a running process is input the guard never sees')
        return Action('other', tool=tool, cwd=cwd)
    if tool.startswith(MCP):
        return Action('mcp', tool=tool, cwd=cwd, params=data if isinstance(data, dict) else None)
    return Action('other', tool=tool, cwd=cwd)
