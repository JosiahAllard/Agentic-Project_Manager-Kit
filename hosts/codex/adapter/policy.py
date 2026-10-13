"""Codex's own hook policy: what only this host needs, judged before kitlib.guard (kitlib.host.hookmain
`screen`). The role policy every host shares is kitlib.guard.rolepolicy; this is what step 4 left in
the Codex branch's guard_adapter and actions.py.

It holds a session the kit marks (PM_KIT_ROLE set; kitlib.sessionmark). The owner's own session,
which nothing marks, is held only by the shared rules, as on every host.

  screen(action, environ) -> Decision or None
    host-tool         a tool call outside ALLOWED_TOOLS (kind other), or an MCP tool other than the
                      lock's read tools: an unknown tool may act in ways no guard rule sees. Agent
                      spawning and messaging tools are outside it until a child's role reaches its
                      hooks (README.md "Not verified").
    host-shell-patch  a shell command that runs apply_patch: a patch sent through the shell is not
                      seen as the edits it makes; the native apply_patch tool is (events.parse).
    host-shell        a call whose shape the guard cannot fully judge (events.CodexAction refusal:
                      no named shell, cmd.exe, a TTY, input fed to a command or a running process).
"""
from kitlib import sessionmark
from kitlib.host import Decision

READ_TOOLS = frozenset({'Read', 'Grep', 'Glob', 'WebSearch', 'WebFetch', 'view_image', 'read_file', 'list_dir',
                        'list_mcp_resources', 'list_mcp_resource_templates', 'read_mcp_resource'})
# Local task bookkeeping and questions to the user: no process, file or peer operation.
BOOKKEEPING_TOOLS = frozenset({'update_plan', 'create_goal', 'update_goal', 'get_goal', 'clock__curr_time',
                               'request_user_input', 'request_user_input_async'})
# write_stdin reaches here only when it sends nothing (a poll); events.parse refuses text.
ALLOWED_TOOLS = READ_TOOLS | BOOKKEEPING_TOOLS | {'write_stdin'}
PATCH_PROGRAMS = ('apply_patch', 'apply-patch')
TAG = '[kit guard: rule=%s role=main]'


def _deny(rule, what, instead):
    return Decision('deny', rule, '%s Instead: %s %s' % (what, instead, TAG % rule), instead)


def lock_read_tools():
    """The lock MCP server's read tools (kitlib.lockmcp.server: every tool not marked a write)."""
    from kitlib.lockmcp import server
    return frozenset(name for name, spec in server.TOOLS.items() if not spec[2])


def lock_read_call(tool):
    """True for one of the lock MCP server's read tools under the server's own name
    (kitlib.lockmcp.tool_prefix for Codex: mcp__machine-lock__<tool>), never another server's
    tool of the same name."""
    from kitlib import lockmcp
    prefix = lockmcp.tool_prefix('codex')
    return isinstance(tool, str) and tool.startswith(prefix) and tool[len(prefix):] in lock_read_tools()


def patch_in_shell(action):
    """The apply_patch program a shell Action runs, or None."""
    from kitlib.guard import commands, rules
    from kitlib.guard import shell as sh
    text = action.command
    if not isinstance(text, str) or not text.strip():
        return None
    parsed = commands.read(text, rules.SHELLS.get(action.shell, sh.BASH), action.workdir or action.cwd)
    for cmd in parsed.commands:
        inv = commands.invocation(cmd)
        if inv is not None and inv.name.lower() in PATCH_PROGRAMS:
            return inv.name
    return None


def screen(action, environ):
    if sessionmark.mark(environ) is None:
        return None                             # the owner's own session
    refusal = getattr(action, 'refusal', '')
    if refusal:
        return _deny('host-shell', 'A Codex agent session runs only calls the kit guard can judge, and %s.' % refusal,
                     'run it as one plain command in PowerShell or Git Bash, or return it to the PM')
    if action.kind == 'other' and action.tool not in ALLOWED_TOOLS:
        return _deny('host-tool', '%s is not a tool the kit has reviewed for a Codex agent session: no guard '
                     'rule can see what it does.' % (action.tool or 'This tool'),
                     'return the operation to the PM, who can do it or add the tool to the kit\'s review')
    if action.kind == 'mcp' and not lock_read_call(action.tool):
        return _deny('host-tool', '%s is not an MCP tool a Codex agent session may call: the kit reviews only the '
                     'machine lock\'s read tools here.' % action.tool,
                     'read the lock with its read tools, or return the operation to the PM')
    if action.kind == 'shell':
        found = patch_in_shell(action)
        if found:
            return _deny('host-shell-patch', '`%s` run through the shell edits files the guard never sees as '
                         'edits.' % found, 'use the apply_patch tool itself')
    return None
