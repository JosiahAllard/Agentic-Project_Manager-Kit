"""The session mark: what kind of session a process runs in (docs/guard.md "Session mark").

The launcher that starts an agent session (a PM, a coordinator, a deep worker) sets PM_KIT_ROLE to
its kind; a subagent is marked by the hook payload's agent_id instead. A session nothing marks is
the owner's own, and counts as the owner for owner-only lock and guard actions.

The mark is read only where the session cannot change it after it starts: the guard hook and the
lock MCP server, which the client starts with the session's environment. The lock CLI does not
read it: an agent sets the environment of every command it runs, so `PM_KIT_ROLE= python ...`
would unmark it. A subagent shares its session's environment, so only the guard (agent_id) tells
it apart.
"""
import os

ENV = 'PM_KIT_ROLE'
# Mark values that would read as a role the guard grants by other means: a launcher that sets one
# gets 'marked-<value>', never the owner's or a subagent's standing.
RESERVED = ('owner', 'subagent')


def mark(environ=None):
    """The session's mark (stripped), or None when the session is unmarked."""
    env = os.environ if environ is None else environ
    value = (env.get(ENV) or '').strip()
    if not value:
        return None
    return 'marked-' + value if value.lower() in RESERVED else value


def owner_check(environ=None):
    """An owner check for kitlib.lock.api.set_owner_check, for a process the session started
    (the lock MCP server): the default check (a caller with no agent host above it), or a caller
    under a known agent host in a session nothing marks. The environment is read once, now. A
    caller whose process tree could not be read (no host known) is not the owner."""
    from kitlib.lock import api
    marked = mark(dict(os.environ if environ is None else environ))

    def check(actor):
        if api.default_owner_check(actor):
            return True
        return marked is None and isinstance(actor, dict) and bool(actor.get('claude'))
    check.marked = marked
    return check
