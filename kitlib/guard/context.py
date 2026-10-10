"""What the guard reads of real state (docs/guard.md "State"; every rule is a read-only
check). Ctx gathers it behind one object so tests hand the rules a fake machine: every reader is
a constructor argument, and the defaults read this machine.

  place(path)        kitlib.guard.place.classify (repo.classify's answer, read from the filesystem
                     where it can be; git otherwise), cached per path. The one change to it: the
                     PM data worktree (docs/pm-data-branch.md "Guard place and commit hooks"),
                     which git calls LINKED, is answered as MAIN, so the main-checkout rules
                     apply there. It is that worktree when the machine config's pm_data.path
                     names its top (kitlib.pmdata.machine_settings), or, only when the machine
                     config names no pm_data, when its HEAD is `ref: refs/heads/<branch>` of the
                     kit-pm-data.json record in its common dir (kitlib.pmdata.read). A MAIN
                     place is never changed.
  is_pm_data(path), pm_data_branch(path)
                     whether place(path) is the PM data worktree, and its data branch
  repo_main_top(path) main_top, except for the PM data worktree: the kit's main checkout it
                     shares a repository with (push and worktree remove judge that repository's
                     refs by its .kit.json; config_at(top) reads one)
  pm_data_error(path) why the guard cannot tell whether path's place is the PM data worktree:
                     the machine config's pm_data does not read (any path), or path's place is a
                     linked worktree whose repository's record or HEAD does not; else None. The
                     rules then deny the subagent write (fail closed).
  config(path)       the .kit.json of the main checkout `path` belongs to (never a worktree's own:
                     an agent cannot loosen the rules from its branch; the PM data worktree,
                     answered as MAIN, is the one worktree whose own .kit.json is read); None
                     when there is none or it does not load (config_error(path) says why)
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

The pm_data argument replaces the machine-config read (a callable returning
kitlib.pmdata.machine_settings's answer, or raising its errors).
"""
import json
import os
import tempfile

from kitlib import config as kconfig
from kitlib import pmdata
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


def _machine_pm_data(home):
    """kitlib.pmdata.machine_settings(home). When the strict reader refuses the machine config,
    its pm_data key alone is read and checked with the same rules, so another key's fault does
    not hide the PM data worktree; a file that cannot be read or parsed raises, and the guard
    fails closed."""
    try:
        return pmdata.machine_settings(home)
    except kconfig.ConfigError as e:
        path = kuser.config_file(home)
        try:
            with open(path, encoding='utf-8-sig') as f:
                data = json.load(f)
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            raise e
        if not isinstance(data, dict):
            raise e
        return _check_pm_data(data.get(pmdata.KEY), '%s key %s' % (path, pmdata.KEY))


def _check_pm_data(cfg, where):
    """kitlib.pmdata.machine_settings's checks of a pm_data value read on its own."""
    if cfg is None:
        return None
    if not isinstance(cfg, dict):
        raise pmdata.PmDataError('%s must be an object' % where)
    extra = set(cfg) - {'path', 'branch', 'remote'}
    if extra:
        raise pmdata.PmDataError('%s: unknown key(s) %s' % (where, ', '.join(sorted(extra))))
    p = cfg.get('path')
    if not isinstance(p, str) or not p.strip() or not os.path.isabs(p):
        raise pmdata.PmDataError('%s: path must be an absolute path, not %r' % (where, p))
    branch = cfg.get('branch', pmdata.DEFAULT_BRANCH)
    remote = cfg.get('remote', pmdata.DEFAULT_REMOTE)
    errs = pmdata.check_fields(branch, remote, '0' * 40)       # a valid root: only the names are checked
    if errs:
        raise pmdata.PmDataError('%s: %s' % (where, '; '.join(errs)))
    return {'path': os.path.normpath(p), 'branch': branch, 'remote': remote}


def _same_dir(a, b):
    return repo.real(a) == repo.real(b) or repo.same_path(a, b)


class Ctx(object):
    def __init__(self, environ=None, home=None, store=None, classify=None, load=None,
                 gate_ok=None, git=None, lock_held=None, role_resolver=None, tempdir=None,
                 pm_data=None):
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
        self._pm_data = pm_data or (lambda: _machine_pm_data(self.home))
        self._pm_settings = None                 # (settings or None, error or None), read once
        self._pm_records = {}                    # normcase(common) -> (Record or None, error or None)
        self._pm_places = {}                     # key of a place answered as the PM data worktree -> its branch
        self._pm_place_errors = {}               # key -> why the place could not be told

    # ------------------------------------------------------------ places and config

    def place(self, path):
        if not path:
            return repo.Place(repo.UNKNOWN, None, 'no path', None, None)
        key = os.path.normcase(os.path.abspath(path))
        if key not in self._places:
            p = self._classify(path)
            branch = self._pm_data_top(key, p) if p.kind == repo.LINKED and p.top else None
            if branch:
                self._pm_places[key] = branch
                p = repo.Place(repo.MAIN, p.top, '', p.git_dir, p.common)
            self._places[key] = p
        return self._places[key]

    def is_pm_data(self, path):
        """True when place(path) is the PM data worktree (answered as MAIN)."""
        return self.pm_data_branch(path) is not None

    def pm_data_branch(self, path):
        """The data branch when place(path) is the PM data worktree, else None."""
        if not path:
            return None
        self.place(path)
        return self._pm_places.get(os.path.normcase(os.path.abspath(path)))

    def repo_main_top(self, path):
        """The main worktree of the repository `path` is in: main_top, except that for the PM
        data worktree it is the kit's main checkout the data worktree belongs to (or None)."""
        p = self.place(path)
        if not self.is_pm_data(path):
            return self.main_top(path)
        if p.common and os.path.basename(p.common).lower() == '.git':
            return os.path.dirname(p.common)
        return None

    def pm_data_error(self, path=None):
        """Why the guard cannot tell the PM data worktree: the machine config's pm_data does not
        read (any path), or `path`'s place is a linked worktree whose repository's record or HEAD
        does not read. None when it can tell."""
        settings, why = self._pm_machine()
        if why:
            return why
        if not path:
            return None
        self.place(path)
        return self._pm_place_errors.get(os.path.normcase(os.path.abspath(path)))

    def _pm_machine(self):
        if self._pm_settings is None:
            try:
                self._pm_settings = (self._pm_data(), None)
            except (pmdata.PmDataError, kconfig.ConfigError) as e:
                self._pm_settings = (None, 'the machine config\'s %s cannot be read: %s' % (pmdata.KEY, e))
        return self._pm_settings

    def _pm_data_top(self, key, p):
        """The data branch when the linked worktree `p` is the PM data worktree (see place), else
        None. A machine config that names pm_data decides alone; the record signal answers only
        when it names none. An error that stops the record signal is kept for pm_data_error."""
        settings, why = self._pm_machine()
        if settings:
            return settings['branch'] if _same_dir(p.top, settings['path']) else None
        if why or not p.common or not p.git_dir:
            return None
        ck = os.path.normcase(p.common)
        if ck not in self._pm_records:
            try:
                self._pm_records[ck] = (pmdata.read(p.common), None)
            except pmdata.PmDataError as e:
                self._pm_records[ck] = (None, str(e))
        record, why = self._pm_records[ck]
        if why:
            self._pm_place_errors[key] = 'the PM data record cannot be read: %s' % why
            return None
        if record is None:
            return None
        head = os.path.join(p.git_dir, 'HEAD')
        try:
            with open(head, encoding='utf-8') as f:
                text = f.read().strip()
        except (OSError, UnicodeDecodeError) as e:
            self._pm_place_errors[key] = 'cannot read %s to compare it with the PM data branch %s: %s' % (
                head, record.branch, e)
            return None
        return record.branch if text == 'ref: refs/heads/%s' % record.branch else None

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
        return self.config_at(self.main_top(path))

    def config_at(self, top):
        """The .kit.json of the main checkout `top` (cached), or None (config_error_at says why)."""
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
        return self.config_error_at(self.main_top(path))

    def config_error_at(self, top):
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
