"""What Codex's start hooks add to the context (SessionStart, PostCompact, SubagentStart): the kit
header (kitlib.host.header) and, for a PM or a coordinator session, the shared start checks
(kitlib.hoststart; from the Codex branch's session_adapter.py).

  text(event, kit, payload, environ, checks, lock_reader)   the context text
  output(adapter, event, kit, stdin, environ)               the hook's stdout (adapter.context)

The role is the session mark (PM_KIT_ROLE, kitlib.sessionmark). A SubagentStart is told that its
role is its parent's mark: Codex starts a child with the parent's environment, and its hooks carry
no child identity, so the child is held to the parent's role policy. After that note every child
gets the agent-start block (kitlib.agentstart) for the payload's cwd, or one could-not-check line
when it raises. A coordinator's external state
note comes from PM_KIT_CODEX_STATE (outside the checkout). The checks read; they register nothing
and start nothing. A check that fails is reported in the text, never raised.
"""
import hashlib
import json
import os
import sys

from kitlib import agentstart, sessionmark
from kitlib.host import header

from hosts.codex.adapter.events import absolute

STATE_ENV = 'PM_KIT_CODEX_STATE'
START_ROLES = ('pm', 'coordinator')
CHILD_NOTE = ('Codex note: this agent runs under its parent session\'s PM_KIT_ROLE mark, and Codex hooks cannot '
              'tell a child from its parent: the kit guard holds every call of a marked session, this one '
              'included, as an agent of that mark\'s role (off the main checkout, inside its own checkout\'s '
              'write roots). The agent name is guidance from the brief, not authority.')

NAMES_SHOWN = 3


def hook_plugin_root(argv=None):
    """The plugin folder the running hook was started from (<root>/boot/hook.py), or None."""
    path = os.path.abspath((argv or sys.argv or [''])[0])
    boot = os.path.dirname(path)
    if os.path.basename(path) == 'hook.py' and os.path.basename(boot) == 'boot':
        return os.path.dirname(boot)
    return None


def _skill_hashes(skills_dir):
    out = {}
    for name in os.listdir(skills_dir):
        f = os.path.join(skills_dir, name, 'SKILL.md')
        if os.path.isfile(f):
            with open(f, 'rb') as fh:
                out[name] = hashlib.sha256(fh.read().replace(b'\r\n', b'\n')).hexdigest()
    return out


def _names(label, names):
    names = sorted(names)
    if not names:
        return ''
    more = ' +%d more' % (len(names) - NAMES_SHOWN) if len(names) > NAMES_SHOWN else ''
    return '%s %s%s' % (label, ', '.join(names[:NAMES_SHOWN]), more)


def stale_line(plugin_root, kit):
    """One line when the plugin's skills differ from the kit's hosts/codex/shell/skills (a plugin
    cache is a copy with no refresh tool), else ''. Reads only the SKILL.md files; never raises."""
    try:
        if not plugin_root:
            return ''
        have = os.path.join(plugin_root, 'skills')
        want = os.path.join(kit, 'hosts', 'codex', 'shell', 'skills')
        if os.path.normcase(os.path.realpath(have)) == os.path.normcase(os.path.realpath(want)):
            return ''
        h, w = _skill_hashes(have), _skill_hashes(want)
        parts = [x for x in (_names('missing:', set(w) - set(h)), _names('extra:', set(h) - set(w)),
                             _names('changed:', [n for n in h if n in w and h[n] != w[n]])) if x]
        if not parts:
            return ''
        return ('Codex plugin copy is behind the kit (%s): refresh the Codex plugin copy (owner step; '
                'see hosts/codex/README.md \'Known limits\').' %'; '.join(parts))
    except Exception as e:
        return 'Codex plugin copy: could not check: %s: %s' % (type(e).__name__, e)


def _lock_lines():
    from kitlib.lock import api
    return api.snapshot(history_days=0)


def text(event, kit, payload=None, environ=None, checks=None, lock_reader=None, plugin_root=None):
    env = os.environ if environ is None else environ
    out = header.text(kit)
    line = stale_line(hook_plugin_root() if plugin_root is None else plugin_root, kit)
    if line:
        out += '\n' + line
    if event == 'SubagentStart':
        # The one call site of the agent-start block (an opt-in setting would go here). Codex
        # hooks carry no child identity, so every child gets it, named a subagent. A block that
        # raises is one line, as at the other hosts' call site (docs/preflight.md "Agent start
        # block"): the header and the note stand.
        try:
            block = agentstart.text((payload or {}).get('cwd'), kit)
        except Exception as e:
            block = 'Agent start: could not check: %s: %s' % (type(e).__name__, e)
        out += '\n' + CHILD_NOTE + '\n' + block
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
