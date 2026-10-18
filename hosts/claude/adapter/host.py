"""Claude Code's kitlib.host.HostAdapter (ARCHITECTURE.md "Hosts"; plan 2.1 and 2.2).

ADAPTER is the one instance; boot/hook.py (`hook.py claude <event> [<part>]`) hands it to
kitlib.host.hookmain.dispatch.

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
  start_text(event_name, kit, payload, environ, part)   the text of one start hook, by its part
                       (PARTS): None the kit header (kitlib.host.header; hooks.json, SessionStart
                       and SubagentStart), and at a SubagentStart of a dispatched kit role the
                       agent-start block after it (kitlib.agentstart, _agent_start); 'coordinator' the coordinator rules
                       (kitlib.coordinatorstart.render, checked in the payload's cwd) in a session
                       marked coordinator, else nothing; 'pm' the PM checklist (kitlib.pmstart; the
                       PM settings' SessionStart hook, docs/pm-start.md). A check that fails says
                       so in the text; it never raises. boot/hook.py writes it; tools/agent_view.py
                       shows it.
  start_parts(event_name, role)   the parts a session of `role` runs at `event_name`: the plugin's
                       (content.HOOKS, every session) and, at a PM's SessionStart, 'pm' (its
                       settings, written by kitlib.machinesetup)
  settings_entry(script, executable)   the PreToolUse entry for settings.json (MATCHER, exec form):
                       `script` (boot/hook.py), then `claude PreToolUse`
  sessions()           hosts.claude.adapter.claude_host, Claude's session files (kitlib.pmwatch's seam)
  command_host(), launcher(), plugin_installer()   None: not behind the contract yet
  content()            hosts.claude.adapter.content (the content renderer)
  push(settings)       the hub's push-notification delivery (hosts.claude.adapter.peer.HubPush:
                       a session's peer pipe; docs/action-hub.md "Push notifications"). Optional:
                       tools/hub.py asks for it with getattr, so a host without it has none

MATCHER is the PreToolUse matcher: exactly the tools a guard rule can refuse (events.FILE_TOOLS,
events.SHELL_TOOLS, events.READ_TOOLS, mcp__*), the same list as hosts.json's hook.guarded_tools,
which boot/hook.py's fast path reads; tests/guard/test_entry.py checks all three agree.
"""
import json

from hosts.claude.adapter import events

MATCHER = 'Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit|Read|Grep|Glob|mcp__.*'
CONTEXT_EVENTS = ('SessionStart', 'SubagentStart')
PARTS = ('coordinator', 'pm')


class ClaudeAdapter(object):
    name = 'claude'
    CONTEXT_EVENTS = CONTEXT_EVENTS

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
    def start_text(event_name, kit, payload=None, environ=None, part=None):
        if part is None:
            from kitlib.host import header
            head = header.text(kit)
            if event_name == 'SubagentStart':
                block = _agent_start(kit, payload, environ)
                if block:
                    return head + '\n' + block
            return head
        if event_name != 'SessionStart' or part not in PARTS:
            raise ValueError('no %s start check %r' % (event_name, part))
        if part == 'coordinator':
            from kitlib import coordinatorstart
            try:
                return coordinatorstart.render(env=environ, cwd=(payload or {}).get('cwd') or None) or ''
            except Exception as e:
                return "Coordinator session start: could not check: %s: %s" % (type(e).__name__, e)
        try:
            from kitlib import pmstart
            cfg = pmstart.load_config()
            if cfg is None:
                from kitlib.config import user
                return "PM session start: no pm_start config in %s; checklist skipped." % user.display()
            return pmstart.checklist(cfg)
        except Exception as e:
            return "PM session start: checklist failed: %s: %s" % (type(e).__name__, e)

    @staticmethod
    def start_parts(event_name, role):
        from hosts.claude.adapter import content
        parts = tuple(part for event, _, part in content.HOOKS if event == event_name)
        return parts + (('pm',) if event_name == 'SessionStart' and role == 'pm' else ())

    def settings_entry(self, script, executable=None):
        from kitlib.host import hookmain
        return hookmain.settings_entry('PreToolUse', MATCHER, script, executable, args=(self.name, 'PreToolUse'))

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

    def push(self, settings=None):
        """The hub's push-notification delivery object (kitlib.hubsite.push: deliver, is_pm):
        hosts.claude.adapter.peer.HubPush over the launcher's settings (`settings`, else
        kitlib.launcher.settings.load()). Raises what loading the settings raises."""
        from hosts.claude.adapter import peer
        if settings is None:
            from kitlib.launcher import settings as launcher_settings
            settings = launcher_settings.load()
        return peer.HubPush(settings)


def _agent_start(kit, payload, environ):
    """The agent-start block (kitlib.agentstart) for a SubagentStart, or None. The role is the
    payload's agent_type when it names a subagent (agent_id); with no agent_id (tools/agent_view.py
    asking for a dispatched role) it is the session mark. Only a dispatched kit role gets the block:
    a host's own agent (Explore) and a session role do not. The one call site: an opt-in setting
    would go here."""
    try:
        from kitlib import agentstart, sessionmark
        act = events.parse('SubagentStart', payload or {})
        role = act.agent_role if act.agent else sessionmark.mark(environ)
        if not agentstart.is_dispatched(role, kit):
            return None
        return agentstart.text(act.cwd, kit, role)
    except Exception as e:
        return 'Agent start: could not check: %s: %s' % (type(e).__name__, e)


ADAPTER = ClaudeAdapter()
