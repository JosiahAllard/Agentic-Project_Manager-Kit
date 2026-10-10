"""What the guard reads of real state (docs/guard.md "State"; every rule is a read-only
check). Ctx gathers it behind one object so tests hand the rules a fake machine: every reader is
a constructor argument, and the defaults read this machine.

  place(path)        kitlib.guard.place.classify (repo.classify's answer, read from the filesystem
                     where it can be; git otherwise), cached per path
  config(path)       the .kit.json of the main checkout `path` belongs to (never a worktree's own:
                     an agent cannot loosen the rules from its branch); None when there is none
                     or it does not load (config_error(path) says why)
  lock_held()        (True, None) only when MACHINE_LOCK_RUN names a run nest_check accepts:
                     the named wrapper is an ancestor of this process, alive, and holding. The
                     variable alone is never proof.
  gate_ok(root, sha) kitlib.gate.runner.verify_record: the record must name a config_rev in
                     sha's history and hash the gate config that commit holds
  user()             ~/.claude-pm-kit/config.json (ledger_path and the guard_* keys), read
                     tolerantly: a file the strict reader refuses only for keys this kit does not
                     know still yields its absolute ledger_path and its string or string-list
                     values. A file that cannot be read, is
                     not a JSON object, or whose ledger_path is not an absolute path string sets
                     'broken' (why), and the ledger rule then denies every subagent write. A
                     missing file is an empty config, not a broken one.
  session_role(ev)   who runs the session (kitlib.sessionmark): 'subagent' when the payload has
                     agent_id, else the launcher's mark (PM_KIT_ROLE), else 'owner': a session
                     nothing marks is the owner's own. Replaceable (role_resolver).
"""
import json
import os
import tempfile

from kitlib import config as kconfig
from kitlib import sessionmark
from kitlib.config import user as kuser
from kitlib.git import refs, repo
from kitlib.guard import place as fsplace

ROLE_ENV = sessionmark.ENV
OWNER = 'owner'
SUBAGENT = 'subagent'


def default_session_role(event, environ):
    """'subagent' when the event carries agent_id; else the launcher's mark (a PM, coordinator or
    deep-worker session it started; kitlib.sessionmark.mark); else 'owner': a session nothing
    marks is the owner's own (docs/guard.md "Session mark")."""
    if (event or {}).get('agent_id'):
        return SUBAGENT
    return sessionmark.mark(environ) or OWNER


def _default_lock_held(environ, store):
    value = environ.get('MACHINE_LOCK_RUN')
    if not value:
        return False, 'MACHINE_LOCK_RUN is not set: this session runs under no lock'
    if os.name != 'nt':
        return False, 'the machine lock is Windows only'
    try:
        from kitlib.lock import run as lockrun
        from kitlib.lock.store import Store
        parent, why = lockrun.nest_check(store if store is not None else Store(), value)
    except Exception as e:                       # an unreadable store proves nothing
        return False, '%s: %s' % (type(e).__name__, e)
    return (True, None) if parent else (False, why)


class Ctx(object):
    def __init__(self, environ=None, home=None, store=None, classify=None, load=None,
                 gate_ok=None, git=None, lock_held=None, role_resolver=None, tempdir=None):
        self.environ = dict(os.environ if environ is None else environ)
        self.home = home or kuser.home_dir()
        self.store = store
        self._classify = classify or fsplace.classify
        self._load = load or kconfig.load
        self._gate_ok = gate_ok
        self.git = git or refs
        self._lock_held = lock_held
        self._role = role_resolver or default_session_role
        self.tempdir = tempdir or self.environ.get('TEMP') or tempfile.gettempdir()
        self._places, self._configs, self._config_errors = {}, {}, {}
        self._user = None

    # ------------------------------------------------------------ places and config

    def place(self, path):
        if not path:
            return repo.Place(repo.UNKNOWN, None, 'no path', None, None)
        key = os.path.normcase(os.path.abspath(path))
        if key not in self._places:
            self._places[key] = self._classify(path)
        return self._places[key]

    def main_top(self, path):
        """The main checkout's work tree root for `path` (a worktree answers its main checkout),
        or None."""
        p = self.place(path)
        if p.kind == repo.MAIN:
            return p.top
        if p.kind == repo.LINKED and p.common:
            return os.path.dirname(p.common) if os.path.basename(p.common).lower() == '.git' else None
        return None

    def config(self, path):
        top = self.main_top(path)
        if not top:
            return None
        key = os.path.normcase(top)
        if key not in self._configs:
            try:
                self._configs[key] = self._load(top)
            except kconfig.ConfigError as e:
                self._configs[key] = None
                self._config_errors[key] = str(e)
        return self._configs[key]

    def config_error(self, path):
        top = self.main_top(path)
        return self._config_errors.get(os.path.normcase(top)) if top else None

    # ------------------------------------------------------------ lock, records

    def lock_held(self):
        if self._lock_held is not None:
            return self._lock_held()
        return _default_lock_held(self.environ, self.store)

    def gate_ok(self, root, sha):
        if self._gate_ok is not None:
            return self._gate_ok(root, sha)
        try:
            from kitlib.gate import runner
            return runner.verify_record(root, sha)
        except Exception as e:
            return None, '%s: %s' % (type(e).__name__, e)

    # ------------------------------------------------------------ the machine config

    def user(self):
        if self._user is None:
            try:
                self._user = kuser.user_config(self.home)
            except kconfig.ConfigError as e:
                self._user = self._tolerant(str(e))
        return self._user

    def _tolerant(self, why):
        out = {k: None for k in kuser.SCHEMA}
        out['error'] = why
        path = kuser.config_file(self.home)
        try:
            with open(path, encoding='utf-8-sig') as f:
                text = f.read()
        except FileNotFoundError:
            return out
        except (OSError, UnicodeDecodeError, ValueError) as e:
            out['broken'] = 'cannot read it: %s' % e
            return out
        try:
            data = json.loads(text)
        except ValueError as e:
            out['broken'] = 'it is not valid JSON: %s' % e
            return out
        if not isinstance(data, dict):
            out['broken'] = 'it must be an object, not %s' % type(data).__name__
            return out
        for k in kuser.SCHEMA:
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v
            elif isinstance(v, list) and v and all(isinstance(x, str) and x.strip() for x in v):
                out[k] = list(v)
        lp = data.get('ledger_path')
        if lp is not None and not isinstance(lp, str):
            out['broken'] = 'ledger_path must be a string, not %r' % (lp,)
        elif isinstance(lp, str) and lp.strip() and not os.path.isabs(lp):
            out['broken'] = 'ledger_path must be an absolute path, not %r' % lp
            out['ledger_path'] = None
        return out

    def session_role(self, event):
        return self._role(event, self.environ)
