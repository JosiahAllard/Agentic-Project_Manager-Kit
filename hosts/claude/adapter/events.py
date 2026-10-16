"""Claude Code's hook payloads read as kitlib.host.Action (docs/guard.md "Output"; plan 2.2).

Every read of a Claude Code hook payload's field names is here, and nowhere in kitlib:

  parse(event_name, payload) -> Action
      PreToolUse: `tool_name` sorts the call. A file tool (FILE_TOOLS: Edit, Write, MultiEdit,
      NotebookEdit) is kind 'edit', its target from the tool's own key (file_path, or
      notebook_path), with `retarget` the same call aimed at `{path}` for an Instead line. A shell
      tool (SHELL_TOOLS: Bash, PowerShell) is kind 'shell': `command`, its dialect, and
      `run_in_background` true as `background`. `mcp__*` is kind 'mcp' (the full name; the guard
      matches it against hosts.json's mcp_tool_format). A read tool (READ_TOOLS: Read, Grep,
      Glob) is kind 'read': Read's file_path, or a search's path (None: cwd) with its terms in
      `params` (Grep's glob as the filter of a contents search, Glob's pattern as the wildcard of
      a names search; Grep's pattern, the text it looks for, is not read). Anything else is
      'other', which no rule judges. SessionStart and SubagentStart are their own kinds.
      `cwd` is passed as sent (None when absent: the guard then uses its own folder). `agent_id`
      is the subagent identity (absent or empty: the main session); `agent_type` becomes
      agent_role with exactly one leading kit plugin prefix dropped (kitlib.agentname.bare,
      docs/guard.md "Agent names"), and is read only for a subagent. PM_KIT_ROLE is not in the
      payload: the guard reads it from its own environment, which Claude Code hands the hook.
  agent(payload)  the agent_id alone, for a deny when parse itself failed (kitlib.host.hookmain)

A payload that is not an object (null, [], 0, "", false included), or a tool_input of the wrong
shape for its tool, raises: the hook denies it (fail closed), as it did when the guard read the
payload itself.
"""
from kitlib import agentname
from kitlib.host import Action

FILE_TOOLS = {'Edit': 'file_path', 'Write': 'file_path', 'MultiEdit': 'file_path',
              'NotebookEdit': 'notebook_path'}
SHELL_TOOLS = {'Bash': 'bash', 'PowerShell': 'powershell'}
# the read tools and the key naming what each reads: the file, or the folder a search starts from
READ_TOOLS = {'Read': 'file_path', 'Grep': 'path', 'Glob': 'path'}
MCP = 'mcp__'
KINDS = {'SessionStart': 'session_start', 'SubagentStart': 'subagent_start'}


def agent(payload):
    return _object(payload).get('agent_id') or None


def _object(payload):
    """`payload` itself when it is a JSON object; anything else (null, a list, a number, a string,
    false) raises, so the hook denies it (fail closed) rather than read it as an empty payload."""
    if not isinstance(payload, dict):
        raise ValueError('a hook payload is a JSON object, not %r' % (payload,))
    return payload


def parse(event_name, payload):
    ev = _object(payload)
    who = agent(ev)
    role = agentname.bare(ev.get('agent_type')) if who else None
    base = dict(cwd=ev.get('cwd'), agent=who, agent_role=role)
    if event_name in KINDS:
        return Action(KINDS[event_name], **base)
    tool = ev.get('tool_name') or ''
    if not isinstance(tool, str):
        raise ValueError('tool_name must be a string, not %r' % (tool,))
    ti = ev.get('tool_input') or {}
    if tool in FILE_TOOLS:
        key = FILE_TOOLS[tool]
        return Action('edit', tool=tool, path=ti.get(key), retarget='%s(%s="{path}")' % (tool, key), **base)
    if tool in SHELL_TOOLS:
        return Action('shell', tool=tool, command=ti.get('command'), shell=SHELL_TOOLS[tool],
                      background=ti.get('run_in_background') is True, **base)
    if tool.startswith(MCP):
        return Action('mcp', tool=tool, params=ti if isinstance(ti, dict) else None, **base)
    if tool in READ_TOOLS:
        return read(tool, ti, base)
    return Action('other', tool=tool, **base)


def read(tool, ti, base):
    """A read tool's call as kind 'read' (kitlib.host.Action's params for a read); a field of the
    wrong type raises, so the hook denies it."""
    path = ti.get(READ_TOOLS[tool])
    terms = {'Grep': ('contents', 'filter', 'glob'), 'Glob': ('names', 'wildcard', 'pattern')}.get(tool)
    params = None
    if terms:
        value = ti.get(terms[2])
        _text(tool, terms[2], value)
        params = {'search': terms[0], terms[1]: value}
    _text(tool, READ_TOOLS[tool], path)
    return Action('read', tool=tool, path=path, params=params, **base)


def _text(tool, key, value):
    if value is not None and not isinstance(value, str):
        raise ValueError('%s %s must be a string, not %r' % (tool, key, value))
