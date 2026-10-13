"""What Codex's start hooks add to the context (SessionStart, PostCompact, SubagentStart): the kit
header (kitlib.host.header) and, for a PM or a coordinator session, the shared start checks
(kitlib.hoststart; from the Codex branch's session_adapter.py).

  text(event, kit, payload, environ, checks, lock_reader)   the context text
  output(adapter, event, kit, stdin, environ)               the hook's stdout (adapter.context)

The role is the session mark (PM_KIT_ROLE, kitlib.sessionmark). A SubagentStart is told that its
role is its parent's mark: Codex starts a child with the parent's environment, and its hooks carry
no child identity, so the child is held to the parent's role policy. A coordinator's external state
note comes from PM_KIT_CODEX_STATE (outside the checkout). The checks read; they register nothing
and start nothing. A check that fails is reported in the text, never raised.
"""
import json

from kitlib import sessionmark
from kitlib.host import header

from hosts.codex.adapter.events import absolute

STATE_ENV = 'PM_KIT_CODEX_STATE'
START_ROLES = ('pm', 'coordinator')
CHILD_NOTE = ('Codex note: this agent runs under its parent session\'s PM_KIT_ROLE mark, and Codex hooks cannot '
              'tell a child from its parent: the kit guard holds every call of a marked session, this one '
              'included, as an agent of that mark\'s role (off the main checkout, inside its own checkout\'s '
              'write roots). The agent name is guidance from the brief, not authority.')


def _lock_lines():
    from kitlib.lock import api
    return api.snapshot(history_days=0)


def text(event, kit, payload=None, environ=None, checks=None, lock_reader=None):
    import os
    env = os.environ if environ is None else environ
    out = header.text(kit)
    if event == 'SubagentStart':
        out += '\n' + CHILD_NOTE
    role = sessionmark.mark(env)
    if role not in START_ROLES or event == 'SubagentStart':
        return out
    cwd = (payload or {}).get('cwd')
    try:
        if not absolute(cwd):
            raise ValueError('a %s start check needs the absolute cwd of the hook payload' % role)
        from kitlib import hoststart
        from kitlib.config import load
        rows = (checks or hoststart.shared_checks)(role, cwd, repo_name=load(cwd).repo.get('name'),
                                                   state_note=env.get(STATE_ENV),
                                                   lock_reader=lock_reader or _lock_lines)
    except Exception as e:
        rows = ('could not check: %s: %s' % (type(e).__name__, e),)
    return out + '\nShared start checks (%s):\n' % role + '\n'.join('- ' + r for r in rows)


def output(adapter, event, kit, stdin, environ=None):
    try:
        payload = json.loads(stdin.read() or '{}')
    except ValueError:
        payload = {}
    return adapter.context(event, text(event, kit, payload if isinstance(payload, dict) else {}, environ))[0]
