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
      - a shell tool (SHELL_TOOLS) is kind shell: the command (`command`, else `cmd`), its dialect
        from `shell` (an explicit PowerShell or Git Bash interpreter; only the PowerShell tool may
        leave it out: the dialect is never guessed from the Windows default), and `workdir` made
        absolute against cwd when the call names one. A TTY, an interactive call or standard input
        handed to the command raises: the guard cannot judge what such a process is told later.
      - write_stdin: kind other when it sends nothing (a poll); text sent to a running process
        raises, for the same reason.
      - mcp__*: kind mcp, the full tool name.
      - anything else: kind other, the tool name kept (hosts.codex.adapter.policy holds a marked
        session to its allowlist).
  agent(payload)  always None: Codex's hooks carry no subagent identity (hosts.json
      capabilities; HOOK_PROTECTION in the README). A child's hook payload names its parent's
      session, and a child inherits its parent's PM_KIT_ROLE, so only the role policy applies.

PM_KIT_ROLE is not in the payload: the guard reads it from the hook's own environment.
"""
import ntpath
import os
import posixpath

from kitlib.host import Action

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
BASHES = {'bash', 'bash.exe', 'sh', 'sh.exe'}
PATCH_HEADERS = ('Add', 'Update', 'Delete')


def agent(payload):
    return None


def absolute(path):
    """True for an absolute path as Windows or POSIX writes one (the hook may be tested off Windows)."""
    return isinstance(path, str) and bool(path) and (ntpath.isabs(path) or posixpath.isabs(path)
                                                     or bool(ntpath.splitdrive(path)[0]))


def shell_kind(tool, data):
    """'powershell' or 'bash' from the call's explicit interpreter; ValueError when it names none
    (except the PowerShell tool) or one the guard cannot read."""
    value = data.get('shell')
    if value is None:
        if tool == 'PowerShell':
            return 'powershell'
        raise ValueError('shell identity is missing; an explicit PowerShell or Git Bash shell is required')
    if not isinstance(value, str):
        raise ValueError('shell must name a known interpreter')
    name = value.replace('\\', '/').rsplit('/', 1)[-1].lower()
    if name in POWERSHELLS:
        return 'powershell'
    if name in BASHES:
        return 'bash'
    raise ValueError('unsupported shell %r; use explicit PowerShell or Git Bash' % value)


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


def _shell(tool, data, cwd):
    if not isinstance(data, dict):
        raise ValueError('shell input must be an object')
    if data.get('tty') not in (None, False) or data.get('interactive') not in (None, False):
        raise ValueError('interactive/TTY execution is unsupported')
    if any(data.get(key) not in (None, '') for key in ('stdin', 'input', 'chars')):
        raise ValueError('shell stdin input is unsupported')
    command = data.get('command', data.get('cmd'))
    if not isinstance(command, str) or not command.strip():
        raise ValueError('shell command is required')
    workdir = data.get('workdir')
    if workdir is not None:
        if not isinstance(workdir, str) or not workdir:
            raise ValueError('shell workdir must be a path')
        workdir = workdir if absolute(workdir) else os.path.normpath(os.path.join(cwd, workdir))
    return Action('shell', tool=tool, command=command, cwd=cwd, shell=shell_kind(tool, data), workdir=workdir)


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
        command = data.get('command') if isinstance(data, dict) else data
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
            raise ValueError('sending stdin to an existing process is unsupported')
        return Action('other', tool=tool, cwd=cwd)
    if tool.startswith(MCP):
        return Action('mcp', tool=tool, cwd=cwd)
    return Action('other', tool=tool, cwd=cwd)
