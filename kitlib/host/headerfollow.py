"""The kit header after a plugin refresh (docs/plugin-source.md "Layout", `headers/`). A session's
header (kitlib.host.header) names the kit folder its start hook ran from: on Claude Code, the plugin
export the marketplace named then. A refresh (kitlib.pluginsource) points the marketplace at a new
export, and the guard's alternatives name the new one (kitlib.guard.rules.advice_kit), while the
session's header still names the old one, which a later prune removes. So the start hook records
which folder a header named, and the tool hook tells the session which folder the kit is now: at
once when the folder its record names is gone (its marker is missing: prune removed it, or claimed
it by renaming), else only when the record is at least EVERY_S old. The record's modified time is
when the session was last named a folder (its start header, or this line), so a session hears the
line at most once an hour however many refreshes land. A record this line rewrote names the export
that was current then, and prune keeps every export replaced less than a day ago
(pluginsource.IN_USE_YOUNG_DAYS), so the hour runs out long before prune can take that folder; a
start header that named an export already old enough to prune is caught by the gone check, at the
first tool event after the prune.

A subagent is the exception: its start hook runs from the folder its session began on, and that
header would be fresh, so the hour would hold the line back for most of a subagent's life while
tools from that folder refuse a .kit.json holding a key newer than the folder. So a subagent's
start header names the current folder outright (start_kit), and is recorded as naming it.

  start_kit(kit, environ)        at a subagent's start: the folder its header names, the one the
                                 guard names (advice_kit) when `kit` and that folder are exports,
                                 else `kit`. Never raises
  note(kit, payload, agent)      at a start event whose text holds the header: when `kit` is an
                                 export (it holds pluginsource.MARKER), write the folder to
                                 <export root>/headers/<key>; at a SessionStart, drop records
                                 older than MAX_AGE_DAYS first. Nothing for a kit checkout
  line(kit, payload, agent, environ)   at a tool event: the line naming the kit folder the guard
                                 names (advice_kit) when this session's or agent's record names
                                 another folder, and that folder is gone or the record is at
                                 least EVERY_S old, and the record then rewritten to it; else None.
                                 No record (a session started from a checkout, or before this
                                 existed): None

`key`: the payload's session_id and the agent (`agent`, the adapter's reading of the payload; None
for the session itself), each a name of letters, digits, `-` and `_`. Both never raise: the line is
advice, and a failure here must not reach the guard's verdict. Logic only: no printing.
"""
import os
import re
import time

HEADERS = 'headers'
MAX_AGE_DAYS = 7
EVERY_S = 3600                   # while its old folder is there, a session hears the line at most this often
_NAME = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


def key(payload, agent=None):
    """`<session_id>-<agent or main>`, or None when either is missing or not a plain name."""
    session = payload.get('session_id') if isinstance(payload, dict) else None
    who = agent or 'main'
    if not isinstance(session, str) or not _NAME.match(session) or not isinstance(who, str) \
            or not _NAME.match(who):
        return None
    return '%s-%s' % (session, who)


def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _prune(folder, now):
    cut = now - MAX_AGE_DAYS * 86400
    for name in os.listdir(folder):
        p = os.path.join(folder, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cut:
                os.remove(p)
        except OSError:
            pass


def _write(path, text):
    tmp = '%s.%d.tmp' % (path, os.getpid())
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    os.replace(tmp, path)


def note(kit, payload, agent=None, event=None, now=None):
    """Record that this session's (or agent's) header named the export `kit`. True when written."""
    try:
        from kitlib import pluginsource
        k = key(payload, agent)
        if not k or not kit or not os.path.isfile(os.path.join(kit, pluginsource.MARKER)):
            return False
        folder = os.path.join(os.path.dirname(os.path.normpath(kit)), HEADERS)
        os.makedirs(folder, exist_ok=True)
        if event == 'SessionStart':
            _prune(folder, time.time() if now is None else now)
        _write(os.path.join(folder, k), os.path.normpath(kit))
        return True
    except Exception:
        return False


def _guard_kit(kit, environ):
    from kitlib.guard import rules
    return rules.advice_kit(_Ctx(kit, os.environ if environ is None else environ))


class _Ctx(object):
    """What kitlib.guard.rules.advice_kit reads: kit, environ, user(). The machine config is read
    from the home the hook's own environment names (USERPROFILE, else HOME), never looked up: an
    environment with neither (a test's) reads no config, so advice_kit names `kit`."""

    def __init__(self, kit, environ):
        self.kit, self.environ = kit, dict(environ or {})

    def user(self):
        home = self.environ.get('USERPROFILE') or self.environ.get('HOME')
        if not home:
            return {'error': 'no home in the hook environment'}
        from kitlib.config import user
        try:
            return user.user_config(home)
        except Exception as e:
            return {'error': str(e)}


def start_kit(kit, environ=None):
    """The folder a subagent's start header names: the current export when `kit` is an export (a
    session run from a kit checkout stays on it), else `kit`, as on any failure: a start never
    fails for this."""
    try:
        from kitlib import pluginsource
        if not kit or not os.path.isfile(os.path.join(kit, pluginsource.MARKER)):
            return kit
        current = _guard_kit(kit, environ)
        return current if current and os.path.isfile(os.path.join(current, pluginsource.MARKER)) else kit
    except Exception:
        return kit


def text(kit):
    """The line a session whose header named an older kit folder is given."""
    k = kit.replace('\\', '/').rstrip('/')
    return ('PM kit header update: the kit plugin was refreshed since your start. The kit folder is now '
            '%s: from here on `<kit>` means that folder, as in `python "%s/tools/preflight.py" -C '
            '<worktree>`; the folder your start header named may be removed.' % (k, k))


def line(kit, payload, agent=None, environ=None, current=None, write=True, now=None):
    """The header-update line for this session or agent, or None: given when the record names
    another folder than the current one and that folder is gone or the record is at least EVERY_S
    old (`now`, tests: the time instead of time.time()). `current` (tests): the folder the guard
    names, instead of advice_kit's reading. `write` False: do not rewrite the record (the caller
    calls again with True once the line has been shown)."""
    try:
        from kitlib import pluginsource
        k = key(payload, agent)
        if not k or not kit:
            return None
        if current is None:
            current = _guard_kit(kit, environ)
            if not current or not os.path.isfile(os.path.join(current, pluginsource.MARKER)):
                return None
        root = os.path.dirname(os.path.normpath(current))
        path = os.path.join(root, HEADERS, k)
        try:
            with open(path, encoding='utf-8') as fh:
                named = fh.read().strip()
        except OSError:
            return None
        if not named or _norm(named) == _norm(current):
            return None
        there = os.path.isfile(os.path.join(named, pluginsource.MARKER))
        if there and (time.time() if now is None else now) - os.path.getmtime(path) < EVERY_S:
            return None                  # its folder is still there, and it was named one lately
        if write:
            _write(path, os.path.normpath(current))
        return text(current)
    except Exception:
        return None
