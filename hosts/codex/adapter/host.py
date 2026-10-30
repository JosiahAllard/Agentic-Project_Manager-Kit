"""Codex's kitlib.host.HostAdapter (ARCHITECTURE.md "Hosts"; plan 2.1 and 2.2).

ADAPTER is the one instance; boot/hook.py (`hook.py codex <event>`, the plugin's copy of it)
hands it to kitlib.host.hookmain.dispatch.

  name                 'codex', its key in hosts.json
  profile, capabilities  hosts.json's "codex" entry, read on first use
  parse(event_name, payload)   hosts.codex.adapter.events.parse (a tuple for apply_patch)
  agent(payload)               None: Codex's hooks carry no subagent identity
  screen(action, environ)      hosts.codex.adapter.policy.screen, Codex's own policy
  render(event_name, decision) (stdout, exit code): nothing for an allow, a warned one too (a
                       warn-mode rule's warning, kitlib.guard.modes, is not shown); for a deny Codex's
                       permission JSON, {"hookSpecificOutput": {"hookEventName",
                       "permissionDecision": "deny", "permissionDecisionReason"}} and a newline,
                       exit 0. Codex has no ask: hookmain lowers one before render, and render
                       lowers one it is still handed (kitlib.host.degrade), never writing an ask.
  context(event_name, text)    (stdout, 0): {"hookSpecificOutput": {"hookEventName",
                       "additionalContext"}} for the CONTEXT_EVENTS; nothing for no text
  start_text(event_name, kit, payload, environ, part)   a start event's text: the kit header and a
                       lead's start checks (hosts.codex.adapter.start); Codex has no named part, so
                       start_parts is not given (one plain hook per start event)
  DEADLINE             seconds the guard has for a whole PreToolUse call, the boot included (a pinned
                       verify hashes every file): under the hooks.json timeout (content.HOOK_TIMEOUT)
  sessions()           AppServerSessions: kitlib.host.SessionHost over the local app-server
                       (hosts.codex.adapter.sessions), each read through a fresh proxy
  command_host()       None: Codex's CommandHost (runtime.SandboxCommandHost) runs a command as one
                       bound native thread, so it is built for that thread by the runtime broker,
                       not handed out here
  launcher()           hosts.codex.adapter.launcher: launch plans only; it runs nothing yet
  plugin_installer()   None
  plugin_copy(home, environ)   optional (tools/_host.py plugin_copies): the step plugin_refresh
                       --apply runs to install the export into Codex
                       (hosts.codex.adapter.plugin_copy.Step; docs/plugin-source.md)
  content()            hosts.codex.adapter.content (the content renderer)
"""
import json

from kitlib.host import degrade

from hosts.codex.adapter import events, policy

CONTEXT_EVENTS = ('SessionStart', 'PostCompact', 'SubagentStart')
DEADLINE = 7


class CodexAdapter(object):
    name = 'codex'
    CONTEXT_EVENTS = CONTEXT_EVENTS
    DEADLINE = DEADLINE

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
    def screen(action, environ):
        return policy.screen(action, environ)

    def render(self, event_name, decision):
        decision = degrade(decision, self.capabilities)
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
    def start_text(event_name, kit, payload=None, environ=None, part=None):
        if part is not None:
            raise ValueError('Codex has no start check %r' % (part,))
        from hosts.codex.adapter import start
        return start.text(event_name, kit, payload, environ)

    def sessions(self):
        from hosts.codex.adapter import sessions
        return sessions.AppServerSessions()

    def command_host(self):
        return None

    def content(self):
        from hosts.codex.adapter import content
        return content

    def launcher(self):
        from hosts.codex.adapter import launcher
        return launcher

    def plugin_installer(self):
        return None

    def plugin_copy(self, home=None, environ=None):
        from hosts.codex.adapter import plugin_copy
        return plugin_copy.Step(home, environ)


ADAPTER = CodexAdapter()
