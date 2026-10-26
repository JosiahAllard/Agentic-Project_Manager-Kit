"""The environment a test child starts from: the session's kit binding dropped, the fences marked.

  KIT_SESSION_ENV        the session's identity and kit binding, never passed to a test child
  session_env_names()    (names, prefixes), upper-cased, of every variable a test child never gets
  scrubbed(environ)      `environ` without them (a copy)
  child_env()            os.environ scrubbed, with the time and home fences' markers

tools/kit_tests.py starts its units, its count child and --list's loader from child_env();
tools/ci_ratchet.py (kitlib/ci/ratchet.py) starts its one child from scrubbed(), and the child's
tests/__init__.py sets the fences itself. Only child_env() loads kitlib.config (the home fence),
and only when it is called, so the ratchet's import chain stays clear of kitlib.config, kitlib.git
and kitlib.proc (docs/test-tools.md "Platform markers and the ratchet")."""
import os

from kitlib import clock
from kitlib import sessionmark
from kitlib.host import registry as hostregistry

# boot/kitboot.py reads PM_KIT_ROOT and PM_KIT_RELEASE_ID (the kit a host entry binds,
# kitlib.kitrelease's pin), kitlib.gate.pin KIT_PINNED (run live, never pin) and
# kitlib.pluginsource.live_root KIT_LIVE_ROOT (the checkout a printed command names); PM_KIT_ROLE
# and PM_KIT_HOST are the session's role and host, PM_KIT_SESSION_NAME its name
# (kitlib.sessionmark). Inherited, they would steer the code under test toward the session's kit;
# a test that wants one sets it itself. Also dropped, read from hosts.json (session_env_names):
# each host's session_env variable and every PM_KIT_<HOST>_ variable (a host adapter's own state
# folder and binary).
KIT_SESSION_ENV = ('PM_KIT_ROOT', 'PM_KIT_RELEASE_ID', 'KIT_LIVE_ROOT', 'KIT_PINNED',
                   'PM_KIT_ROLE', 'PM_KIT_HOST', sessionmark.SESSION_NAME_ENV)


def session_env_names():
    """`(names, prefixes)`, upper-cased, of the variables a test child never gets: KIT_SESSION_ENV
    and every registered host's session_env variable (hosts.json), and the prefix PM_KIT_<HOST>_
    of each registered host; only KIT_SESSION_ENV when hosts.json cannot be read."""
    try:
        hosts = hostregistry.union('session_env')
        prefixes = tuple('PM_KIT_%s_' % n.upper() for n in hostregistry.load())
    except hostregistry.RegistryError:
        hosts, prefixes = (), ()
    return {n.upper() for n in KIT_SESSION_ENV + hosts}, prefixes


def scrubbed(environ):
    """A copy of `environ` without session_env_names()."""
    names, prefixes = session_env_names()
    return {k: v for k, v in environ.items()
            if k.upper() not in names and not k.upper().startswith(prefixes)}


def child_env():
    """The environment every test child of kit_tests starts from (the units, the count child,
    --list's loader): this process's without session_env_names(), with the home fence's marker
    (kitlib.config.user.fence_env: a pool worker (kitlib/tooldeps/pool_worker.py) replaces its
    environment for each unit, so tests/__init__.py's marker would not reach it), and the time
    fence's marker (kitlib.clock.FENCE_ENV = '1' unless set, for the same reason)."""
    from kitlib.config import user as kuser         # kitlib.config loads git and proc: see above
    env = scrubbed(os.environ)
    env.setdefault(clock.FENCE_ENV, '1')
    return kuser.fence_env(env)
