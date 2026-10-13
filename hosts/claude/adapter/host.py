"""Claude Code's kitlib.host.HostAdapter (ARCHITECTURE.md "Hosts"; plan 2.1 and 2.2).

ADAPTER is the one instance; hosts/claude/shell/hooks/guard.py hands it to kitlib.host.hookmain.run.

  name                 'claude', its key in hosts.json
  profile, capabilities  hosts.json's "claude" entry (kitlib.host.registry), read on first use so
                       an unguarded call never reads the file
  parse(event_name, payload)   hosts.claude.adapter.events.parse
  agent(payload)               hosts.claude.adapter.events.agent
  render(event_name, decision) (stdout, exit code): nothing for an allow; for a deny or an ask
                       Claude Code's hook JSON, {"hookSpecificOutput": {"hookEventName",
                       "permissionDecision", "permissionDecisionReason"}} and a newline. Always
                       exit 0: the verdict is in the JSON (docs/guard.md "Output").
  context(event_name, text)    (stdout, 0): text a session or subagent start hook adds to the
                       agent's context, {"hookSpecificOutput": {"hookEventName",
                       "additionalContext"}} and a newline; nothing for no text. Only the
                       CONTEXT_EVENTS take it.
  settings_entry(script, executable)   the PreToolUse entry for settings.json (MATCHER, exec form)
  start_text(event_name, kit, payload, environ)   hosts.claude.adapter.start.text: what its start
                       hooks add to the context, the header first
  sessions()           hosts.claude.adapter.claude_host, Claude's session files (kitlib.pmwatch's seam)
  command_host(), launcher(), plugin_installer()   None: not behind the contract yet
  content()            hosts.claude.adapter.content (the content renderer)

MATCHER is the PreToolUse matcher: exactly the tools a guard rule can refuse (events.FILE_TOOLS,
events.SHELL_TOOLS, mcp__*); tests/guard/test_entry.py checks it against them.
"""
import json

from hosts.claude.adapter import events

MATCHER = 'Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit|mcp__.*'
CONTEXT_EVENTS = ('SessionStart', 'SubagentStart')


class ClaudeAdapter(object):
    name = 'claude'

    def __init__(self, registry_path=None):
        self._path, self._host = registry_path, None

    def _entry(self):
        if self._host is None:
            from kitlib.host import registry
            self._host = registry.host(self.name, self._path)
        return self._host

    @property
    def profile(self):
        return self._entry().profile

    @property
    def capabilities(self):
        return self._entry().capabilities

    @staticmethod
    def parse(event_name, payload):
        return events.parse(event_name, payload)

    @staticmethod
    def agent(payload):
        return events.agent(payload)

    @staticmethod
    def render(event_name, decision):
        if decision.verdict == 'allow':
            return '', 0
        return json.dumps({'hookSpecificOutput': {'hookEventName': event_name,
                                                  'permissionDecision': decision.verdict,
                                                  'permissionDecisionReason': decision.reason}}) + '\n', 0

    @staticmethod
    def context(event_name, text):
        if event_name not in CONTEXT_EVENTS:
            raise ValueError('%s carries no additionalContext' % event_name)
        if not text:
            return '', 0
        return json.dumps({'hookSpecificOutput': {'hookEventName': event_name,
                                                  'additionalContext': text}}) + '\n', 0

    @staticmethod
    def settings_entry(script, executable=None):
        from kitlib.host import hookmain
        return hookmain.settings_entry('PreToolUse', MATCHER, script, executable)

    @staticmethod
    def start_text(event_name, kit, payload=None, environ=None):
        from hosts.claude.adapter import start
        return start.text(event_name, kit, payload, environ)

    def sessions(self):
        from hosts.claude.adapter import claude_host
        return claude_host

    def command_host(self):
        return None

    def content(self):
        from hosts.claude.adapter import content
        return content

    def launcher(self):
        return None

    def plugin_installer(self):
        return None


ADAPTER = ClaudeAdapter()
