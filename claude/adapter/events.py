"""Claude Code's hook payloads read as kitlib.host.Action (docs/guard.md "Output"; plan 2.2).

Every read of a Claude Code hook payload's field names is here, and nowhere in kitlib:

  parse(event_name, payload) -> Action
      PreToolUse: `tool_name` sorts the call. A file tool (FILE_TOOLS: Edit, Write, MultiEdit,
      NotebookEdit) is kind 'edit', its target from the tool's own key (file_path, or
      notebook_path), with `retarget` the same call aimed at `{path}` for an Instead line. A shell
      tool (SHELL_TOOLS: Bash, PowerShell) is kind 'shell': `command`, its dialect, and
      `run_in_background` true as `background`. `mcp__*` is kind 'mcp' (the full name; the guard
      matches it against hosts.json's mcp_tool_format). Anything else is 'other', which no rule
      judges. SessionStart and SubagentStart are their own kinds.
      `cwd` is passed as sent (None when absent: the guard then uses its own folder). `agent_id`
      is the subagent identity (absent or empty: the main session); `agent_type` becomes
      agent_role with exactly one leading kit plugin prefix dropped (kitlib.agentname.bare,
      docs/guard.md "Agent names"), and is read only for a subagent. PM_KIT_ROLE is not in the
      payload: the guard reads it from its own environment, which Claude Code hands the hook.
  agent(payload)  the agent_id alone, for a deny when parse itself failed (kitlib.host.hookmain)

A payload that is not an object, or a tool_input of the wrong shape for its tool, raises: the hook
denies it (fail closed), as it did when the guard read the payload itself.
"""
from kitlib import agentname
from kitlib.host import Action

FILE_TOOLS = {'Edit': 'file_path', 'Write': 'file_path', 'MultiEdit': 'file_path',
              'NotebookEdit': 'notebook_path'}
SHELL_TOOLS = {'Bash': 'bash', 'PowerShell': 'powershell'}
MCP = 'mcp__'
KINDS = {'SessionStart': 'session_start', 'SubagentStart': 'subagent_start'}


def agent(payload):
    return (payload or {}).get('agent_id') or None


def parse(event_name, payload):
    ev = payload or {}
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
        return Action('mcp', tool=tool, **base)
    return Action('other', tool=tool, **base)
