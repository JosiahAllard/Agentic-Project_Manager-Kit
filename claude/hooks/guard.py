"""guard.py: the kit's PreToolUse hook (ARCHITECTURE.md "Guard rules"; docs/guard.md).

Reads the PreToolUse event JSON on stdin, asks kitlib.guard.decide, and prints the decision as
Claude Code's hook JSON (a deny or an ask; nothing for an allow). It always exits 0: the decision
is in the JSON. A call no rule can refuse (any tool but the GUARDED ones and mcp__*) is
allowed before kitlib is even imported. For every other call, a guard that fails, a kitlib that
will not import included, denies (fail closed), names the error and the command that
reproduces it. If even that cannot be written it exits 2, which Claude Code also reads as a block
(under pythonw too, where there may be no stderr to say why).

  python claude/hooks/guard.py < event.json
  python claude/hooks/guard.py --settings-entry    the PreToolUse entry for settings.json

The settings entry is exec form: Claude Code starts pythonw.exe on guard.py directly, with no
shell and no console (docs/guard.md "Go-live"). The interpreter is the pythonw beside the python
that runs --settings-entry, so the path is this machine's, worked out at run time; the kit
holds none.
"""
import json
import os
import sys

ENTRY = os.path.abspath(__file__).replace(os.sep, '/')


def failed(event, e):
    """The deny JSON for a guard that failed, built by hand so it needs nothing from kitlib."""
    role = 'subagent' if isinstance(event, dict) and event.get('agent_id') else 'main'
    reason = ('The kit guard failed (%s: %s), so it cannot clear this call. Reproduce it with the event JSON '
              'this call sent and report the error to the PM; do not reword the call. Instead: python "%s" '
              '< event.json [kit guard: rule=guard-error role=%s]' % (type(e).__name__, e, ENTRY, role))
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                   'permissionDecisionReason': reason}}


GUARDED = frozenset(('Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Bash', 'PowerShell'))
MATCHER = 'Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit|mcp__.*'


def unguarded(event):
    """True for a call no rule can refuse, known before kitlib is imported: kitlib.guard.decide
    answers None for every tool that is not a file tool (Edit, Write, MultiEdit, NotebookEdit), a
    shell tool (Bash, PowerShell) or an MCP tool (mcp__*). GUARDED must stay the union of
    kitlib/guard/rules.py FILE_TOOLS and SHELL_TOOLS (tests/guard/test_entry.py checks it)."""
    if not isinstance(event, dict):
        return False
    tool = event.get('tool_name')
    return isinstance(tool, str) and tool not in GUARDED and not tool.startswith('mcp__')


def main(stdin, stdout):
    event = None
    try:
        event = json.loads(stdin.read() or '{}')
        if unguarded(event):
            return 0                                               # Read, Grep, Glob...: nothing to decide
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if sys.path[:1] != [root]:
            sys.path.insert(0, root)
        from kitlib import guard
        out = guard.hook_output(guard.decide(event))
    except Exception as e:                                         # fail closed, every tool
        out = failed(event, e)
    if out is not None:
        stdout.write(json.dumps(out))
        stdout.write('\n')
    return 0


def windowless(executable=None):
    """The pythonw.exe beside `executable` (default: this interpreter), else `executable` itself."""
    exe = executable or sys.executable
    w = os.path.join(os.path.dirname(exe), 'pythonw.exe')
    return w if os.name == 'nt' and os.path.isfile(w) else exe


def settings_entry(executable=None):
    """{"PreToolUse": [...]} for settings.json "hooks": exec form (command + args, no shell)."""
    return {'PreToolUse': [{'matcher': MATCHER, 'hooks': [
        {'type': 'command', 'command': windowless(executable).replace(os.sep, '/'), 'args': [ENTRY]}]}]}


if __name__ == '__main__':
    if sys.argv[1:] == ['--settings-entry']:
        sys.stdout.write(json.dumps(settings_entry(), indent=2))
        sys.stdout.write(chr(10))
        sys.exit(0)
    try:
        sys.exit(main(sys.stdin, sys.stdout))
    except SystemExit:
        raise
    except BaseException as e:                                     # nothing printed: block by exit code
        try:                                                       # pythonw may have no stderr at all
            sys.stderr.write('kit guard failed: %s: %s\n' % (type(e).__name__, e))
        except BaseException:
            pass
        sys.exit(2)
