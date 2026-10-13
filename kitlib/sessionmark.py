"""The session mark: what kind of session a process runs in (docs/guard.md "Session mark").

The launcher that starts an agent session (a PM, a coordinator, a deep worker) sets PM_KIT_ROLE to
its kind; a subagent is marked by the hook payload's agent_id instead. A session nothing marks is
the owner's own, and counts as the owner for owner-only lock and guard actions.

The mark is read only where the session cannot change it after it starts: the guard hook and the
lock MCP server, which the client starts with the session's environment. The lock CLI does not
read it: an agent sets the environment of every command it runs, so `PM_KIT_ROLE= python ...`
would unmark it. A subagent shares its session's environment, so only the guard (agent_id) tells
it apart.

A mark that carries the kit plugin's agent prefix (kitlib.agentname.bare_mark) is read without it,
before the reserved check: `<plugin>:coordinator` is the coordinator's mark, `<plugin>:owner` is
'marked-owner'.

  child_environment(environ, host, additions, preserve_lock, registry_path)
        the environment for a new session a process launches (docs/multi-host-plan.md section 2.1):
        `environ` without the session's mark (PM_KIT_ROLE, PM_KIT_SESSION_NAME), without every
        registered host's session id variable (hosts.json profile session_env), without the
        variables of every host but `host` (profile env_prefixes; host None: every host's), and
        without the lock's nest token (MACHINE_LOCK_*) unless `preserve_lock` (the same session's
        own next tool, whose lock the shared lock verifies); then `additions`. Names are compared
        without case, as Windows does. So a marked parent on one host cannot hand its role or its
        session to a child on another.
"""
import os

from kitlib import agentname

ENV = 'PM_KIT_ROLE'
# Mark values that would read as a role the guard grants by other means: a launcher that sets one
# gets 'marked-<value>', never the owner's or a subagent's standing.
RESERVED = ('owner', 'subagent')


def mark(environ=None):
    """The session's mark (stripped, the kit plugin's prefix removed), or None when the session is
    unmarked."""
    env = os.environ if environ is None else environ
    value = (env.get(ENV) or '').strip()
    if not value:
        return None
    value = agentname.bare_mark(value)          # never empty: a value that is all prefix stays as it is
    return 'marked-' + value if value.lower() in RESERVED else value


def owner_check(environ=None):
    """An owner check for kitlib.lock.api.set_owner_check, for a process the session started
    (the lock MCP server): the default check (a caller with no agent host above it), or a caller
    under a known agent host in a session nothing marks. The environment is read once, now. A
    caller whose process tree could not be read (no host known) is not the owner."""
    from kitlib.lock import api, store
    marked = mark(dict(os.environ if environ is None else environ))

    def check(actor):
        if api.default_owner_check(actor):
            return True
        return marked is None and bool(store.actor_host(actor))
    check.marked = marked
    return check


SESSION_NAME_ENV = 'PM_KIT_SESSION_NAME'
LOCK_PREFIX = 'MACHINE_LOCK_'


def child_environment(environ, host=None, additions=None, preserve_lock=False, registry_path=None):
    from kitlib.host import registry
    hosts = registry.load(registry_path)
    if host is not None and host not in hosts:
        raise registry.RegistryError('unknown host %r' % (host,))
    names = {ENV, SESSION_NAME_ENV} | {h.profile.session_env.upper() for h in hosts.values()}
    prefixes = tuple(p for h in hosts.values() if h.name != host for p in h.profile.env_prefixes)
    if not preserve_lock:
        prefixes += (LOCK_PREFIX,)
    out = {k: v for k, v in environ.items()
           if k.upper() not in names and not k.upper().startswith(prefixes)}
    out.update(additions or {})
    return out
