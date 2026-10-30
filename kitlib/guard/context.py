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
  pm_data_named()    the machine config names pm_data (or cannot be read)
  pm_data_path()     the machine config's pm_data.path, or None
  pm_root()          the root commit in the record of the machine config's pm_data repository
                     ((None, None) when it names none or that repository has no record;
                     (None, why) when either cannot be read)
  config(path)       the .kit.json of the main checkout `path` belongs to (never a worktree's own:
                     an agent cannot loosen the rules from its branch; the PM data worktree,
                     answered as MAIN, is the one worktree whose own .kit.json is read); a
                     checkout with no .kit.json answers the defaults (kitlib.config.load: main
                     branch and name from its git files, owner email from the machine config
                     under `home`); None when there is no checkout or it does not load
                     (config_error(path) says why)
  lock_held()        (True, None) only when MACHINE_LOCK_RUN names a run nest_check accepts:
                     the named wrapper is an ancestor of this process, alive, and holding. The
                     variable alone is never proof.
  gate_ok(root, sha) kitlib.gate.runner.verify_record: the record must name a config_rev in
                     sha's history and hash the gate config that commit holds; a record's .git
                     copy counts only when there is no record folder (no dev_drive;
                     kitlib.records.gate_ok.read)
  user()             the machine config (ledger_path, ledger_dir and the guard_* keys), read
                     tolerantly: files the strict reader refuses only for keys this kit does not
                     know still yield their ledger_path, ledger_dir and their string or
                     string-list values, each key from its own file (kitlib.config.user
                     POLICY_KEYS from config.json, LOCAL_KEYS from local.json) and a relative
                     ledger_path or ledger_dir read against the PM repository. A file that cannot
                     be read or is not a JSON object, or a ledger_path or ledger_dir that is not a
                     path string or is blank (the key is then unset), or that is not absolute
                     once read (`C:books`), or a ledger_dir that exists and is not a folder (both
                     still held as given),
                     sets 'broken' (why), and the ledger rule then denies every subagent write
                     (_ledger_checked, on the strict answer too). A ledger_dir not made yet, or
                     outside the PM repository, is accepted. Missing files are an empty config, not
                     a broken one. The files are the ones config_location() names; an UNMOVED,
                     LEFTOVER or BAD location sets 'broken' too (fail closed); an UNMOVED one's
                     home leftover is still read, and a LEFTOVER one's PM repository config.json
                     for its ledger_path and ledger_dir only, so the ledger stays protected; a
                     BAD one's are read the same way from <pointer>/.pm-kit/config.json when
                     the PM-repo pointer's value is absolute (_bad_ledger_keys). A ledger key the
                     guard could not read, so that the ledger may be at any path (a file that does
                     not read, a BAD location whose pointer config gives nothing, a blank,
                     non-string or unplaceable relative key), also sets 'ledger_unread' (why): the
                     ledger rule then asks the owner about every main-session write
                     (rules.ledger_unread).
  ledger_files()     kitlib.ledger.files.of_config over user(): where the ledger's log files,
                     standing digest and rulebooks' folder are (the ledger rule), or None
  config_location()  kitlib.config.user.locate(home) with the PM-repo pointer read once from the
                     registry (kitlib.config.pointer.read_raw; pm_repo_pointer shares that read)
                     and checked by kitlib.config.pointer.read with place()'s classify and the
                     branch read from the work tree's HEAD file, so a hook call starts no git for
                     it when the filesystem answers; taken once per Ctx: every read of the
                     machine config in one hook call reads the same files.
  kit                the kit the guard runs from (kitlib.kitroot.root(): the checkout this module
                     is in), or a test's folder; the lock tool an alternative names is under it
                     (rules.lock_tool).
  session_role(a)    who runs the session of the kitlib.host.Action `a` (kitlib.sessionmark):
                     'subagent' when the host named a subagent (Action.agent), else the launcher's
                     mark (PM_KIT_ROLE: Action.role when the host hands it over, else this
                     environment's), else 'owner': a session nothing marks is the owner's own.
                     Replaceable (role_resolver, called with the Action and the environment).
  host_profile()     the kitlib.host.HostProfile of the host whose hook asked (the `profile`
                     argument, or a callable returning it, which kitlib.host.hookmain passes from
                     its adapter), else the one PM_KIT_HOST
                     selects in hosts.json (kitlib.host.registry), read once. Only an MCP call
                     reads it (rules.mcp_prefix).

The pm_data argument replaces the machine-config read (a callable returning
kitlib.pmdata.machine_settings's answer, or raising its errors). The pointer argument replaces the
PM-repo pointer value pm_repo_pointer answers (a callable returning it or None), and only that:
the machine config is still read through the registry's pointer. The location argument replaces
config_location (a callable returning a kitlib.config.user.Location).
"""
import json
import os
import tempfile

from kitlib import config as kconfig
from kitlib import kitroot
from kitlib import pmdata
from kitlib import sessionmark
from kitlib.config import pointer as kpointer
from kitlib.config import user as kuser
from kitlib.git import refs, repo
from kitlib.guard import paths
from kitlib.guard import place as fsplace

ROLE_ENV = sessionmark.ENV
OWNER = 'owner'
SUBAGENT = 'subagent'
LEDGER_KEYS = ('ledger_path', 'ledger_dir')     # the paths the ledger rule protects (_tolerant)


def default_session_role(action, environ):
    """'subagent' when the Action names a subagent (Action.agent); else the launcher's mark (a PM,
    coordinator or deep-worker session it started; kitlib.sessionmark.mark), from Action.role when
    the host handed it over, else from `environ`; else 'owner': a session nothing marks is the
    owner's own (docs/guard.md "Session mark")."""
    if getattr(action, 'agent', None):
        return SUBAGENT
    role = getattr(action, 'role', None)
    return sessionmark.mark(environ if role is None else {sessionmark.ENV: role}) or OWNER


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


def _machine_pm_data(home, location=None):
    """kitlib.pmdata.machine_settings(home): the pm_data the PM-repo pointer derives
    (kitlib.config.user.Location.pm_data), which no key of the machine config's files can hide; an
    UNMOVED or BAD `location` (kitlib.config.user.locate) raises, and the guard fails closed."""
    return pmdata.machine_settings(home, location=location or kuser.locate(home))


def _tolerant_read(path):
    """(the JSON object in `path`, None), ({}, None) when it is absent, or ({}, why) when it cannot
    be read or is not a JSON object."""
    try:
        with open(path, encoding='utf-8-sig') as f:
            text = f.read()
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return {}, 'cannot read it: %s' % e
    try:
        data = json.loads(text)
    except ValueError as e:
        return {}, 'it is not valid JSON: %s' % e
    if not isinstance(data, dict):
        return {}, 'it must be an object, not %s' % type(data).__name__
    return data, None


def _placed(path):
    """`path` names one place whatever folder the hook runs in: absolute, with a drive or a UNC
    share (`C:books` and a rooted `/books` depend on the current folder). On Linux (paths.posix):
    absolute, starting with `/`; a relative or empty path is not placed (fail closed)."""
    if paths.posix():
        return isinstance(path, str) and path.startswith('/')
    return os.path.isabs(path) and bool(os.path.splitdrive(path)[0])


def _not_a_folder(path):
    """`path` exists and is not a folder, read without its trailing separators (with one, Windows
    answers a file as absent); a drive root keeps its own."""
    bare = path.rstrip('\\/')
    if not bare or bare.endswith(':'):
        bare = path
    return os.path.exists(bare) and not os.path.isdir(bare)


def _unread(out, why):
    """Mark `out` (a user() answer) as holding a ledger key the guard could not read: 'broken' and
    'ledger_unread' each set to `why` unless already set (the first cause is kept)."""
    if not out.get('broken'):
        out['broken'] = why
    if not out.get('ledger_unread'):
        out['ledger_unread'] = why


def _ledger_keys(data, pm, out):
    """LEDGER_KEYS of the JSON object `data` into `out`: a relative path read against the PM
    repository `pm`; a value that is not a path string, is blank, or is relative with no `pm`
    leaves the key unset and sets 'broken' and 'ledger_unread' (why)."""
    for k in LEDGER_KEYS:
        v = data.get(k)
        if v is None:
            continue
        why = None
        if not isinstance(v, str):
            why = '%s must be a string, not %r' % (k, v)
        elif not v.strip():
            why = '%s must be a non-empty path, not %r' % (k, v)
        elif os.path.isabs(v):
            out[k] = v
        elif pm:
            out[k] = kuser.resolved({k: v}, pm)[k]
        else:
            why = '%s must be an absolute path, not %r' % (k, v)
        if why:
            out['broken'], out[k] = why, None
            _unread(out, why)


def _ledger_checked(u):
    """`u` (a user() answer) with each ledger key checked: a blank one is unset (nothing to hold)
    and sets 'broken' and 'ledger_unread'; one that is not absolute once read (`C:books`) and a ledger_dir that exists
    and is not a folder (a trailing separator dropped first: `<ledger_dir>\\` names the
    file `<ledger_dir>`) set 'broken' and are still held as given, so no session writes what the guard
    held before. A folder not made yet (ledger.py makes it) or outside the PM repository is
    accepted. An earlier 'broken' is kept: it is the first cause."""
    for k in LEDGER_KEYS:
        v = u.get(k)
        why = None
        if v is None:
            continue
        if not isinstance(v, str) or not v.strip():
            why, u[k] = '%s must be a non-empty path, not %r' % (k, v), None
            _unread(u, why)
        elif not _placed(v):
            why = '%s must be an absolute path, not %r' % (k, v)
        elif k == 'ledger_dir' and _not_a_folder(v):
            why = 'ledger_dir %s is not a folder' % v
        if why and not u.get('broken'):
            u['broken'] = why
    return u


def _same_dir(a, b):
    return repo.real(a) == repo.real(b) or repo.same_path(a, b)


class Ctx(object):
    def __init__(self, environ=None, home=None, store=None, classify=None, load=None,
                 gate_ok=None, git=None, lock_held=None, role_resolver=None, tempdir=None,
                 pm_data=None, kit=None, profile=None, pointer=None, location=None):
        self.environ = dict(os.environ if environ is None else environ)
        self._pointer_read = pointer             # None: the registry (kitlib.config.pointer.read_raw)
        self._pointer = None                     # pm_repo_pointer's (value or None, why or None)
        self._registry = None                    # read_raw's answer, read once
        self._location_read = location
        self.home = home or kuser.home_dir()
        self.kit = kit or kitroot.root()
        self.store = store
        self._classify = classify or fsplace.classify
        self._load = load or (lambda top: kconfig.load(top, home=self.home, machine=self.user))
        self._gate_ok = gate_ok
        self.git = git or refs
        self._lock_held = lock_held
        self._role = role_resolver or default_session_role
        self._profile = profile
        self.tempdir = tempdir or self.environ.get('TEMP') or tempfile.gettempdir()
        self._places, self._configs, self._config_errors = {}, {}, {}
        self._raw_places = {}                    # key -> self._classify's answer, unadjusted
        self._user = None
        self._location = None
        self._pm_data = pm_data or (lambda: _machine_pm_data(self.home, self.config_location()))
        self._pm_settings = None                 # (settings or None, error or None), read once
        self._pm_records = {}                    # paths.norm(common) -> (Record or None, error or None)
        self._pm_places = {}                     # key of a place answered as the PM data worktree -> its branch
        self._pm_place_errors = {}               # key -> why the place could not be told

    # ------------------------------------------------------------ places and config

    def _raw(self, path):
        """self._classify(path), cached: place's answer before its PM data change (the pointer
        check reads it; place itself needs the machine config, so it cannot)."""
        key = paths.norm(path, absolute=True)
        if key not in self._raw_places:
            self._raw_places[key] = self._classify(path)
        return self._raw_places[key]

    def _head_branch(self, path):
        """refs.current_branch's answer for the work tree at `path`, read from its HEAD file
        (_raw's git_dir) when that names a branch or a commit, else from git."""
        p = self._raw(path)
        answer = fsplace.head_branch(p.git_dir) if p.git_dir else None
        return answer if answer is not None else self.git.current_branch(path)

    def place(self, path):
        if not path:
            return repo.Place(repo.UNKNOWN, None, 'no path', None, None)
        key = paths.norm(path, absolute=True)
        if key not in self._places:
            p = self._raw(path)
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
        return self._pm_places.get(paths.norm(path, absolute=True))

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
        return self._pm_place_errors.get(paths.norm(path, absolute=True))

    def pm_data_named(self):
        """True when the machine config names pm_data, or cannot be read (fail closed)."""
        settings, why = self._pm_machine()
        return bool(settings) or bool(why)

    def pm_data_path(self):
        """The machine config's pm_data.path, or None (none named, or it cannot be read)."""
        settings, _why = self._pm_machine()
        return settings['path'] if settings else None

    def pm_repo_pointer(self):
        """The PM-repo pointer's value as stored (kitlib.config.pointer.read_raw), read once, or
        None when there is none or it cannot be read. It is not checked: a rule that protects a
        file under it holds whatever folder it names."""
        return self._pointer_answer()[0] or None

    def _pointer_answer(self):
        """The registry's answer (_registry_raw), or the pointer argument's value as (value or
        None, None); read once."""
        if self._pointer is None:
            if self._pointer_read is None:
                self._pointer = self._registry_raw()
            else:
                self._pointer = (self._pointer_read() or None, None)
        return self._pointer

    def _registry_raw(self):
        """kitlib.config.pointer.read_raw's answer, read once."""
        if self._registry is None:
            self._registry = kpointer.read_raw()
        return self._registry

    def pm_root(self):
        """The machine's PM data record as (root sha, None); (None, None) when the machine config
        names no pm_data or its repository has no record; (None, why) when the machine config or
        the record cannot be read, or pm_data.path is no checkout git can place. The record is the
        one in the git common dir of pm_data.path."""
        settings, why = self._pm_machine()
        if why:
            return None, why
        if not settings:
            return None, None
        p = self.place(settings['path'])
        if p.kind not in (repo.MAIN, repo.LINKED) or not p.common:
            return None, 'the PM data worktree %s is not a checkout git can place (%s)' % (
                settings['path'], p.detail or p.kind)
        try:
            record = pmdata.read(p.common)
        except pmdata.PmDataError as e:
            return None, str(e)
        if record is None:
            return None, None                   # no record yet: no data branch to guard
        return record.root, None

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
        ck = paths.norm(p.common)
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
        key = paths.norm(top)
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
        return self._config_errors.get(paths.norm(top)) if top else None

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

    def config_location(self):
        if self._location is None:
            if self._location_read is not None:
                self._location = self._location_read()
            else:
                ptr = kpointer.read(raw=self._registry_raw(), classify=self._raw, branch=self._head_branch)
                self._location = kuser.locate(self.home, ptr=ptr)
        return self._location

    def user(self):
        if self._user is None:
            try:
                self._user = kuser.user_config(self.home, location=self.config_location())
            except kconfig.ConfigError as e:
                self._user = self._tolerant(str(e))
            self._user = _ledger_checked(self._user)
        return self._user

    def ledger_files(self):
        from kitlib.ledger import files
        return files.of_config(self.user())

    def _tolerant(self, why):
        out = {k: None for k in kuser.SCHEMA}
        out['error'] = why
        loc = self.config_location()
        if loc.error and loc.state not in (kuser.UNMOVED, kuser.LEFTOVER):
            if loc.state != kuser.BAD or not self._bad_ledger_keys(out):
                _unread(out, loc.error)
            out['broken'] = loc.error
            return out
        if loc.state == kuser.OK:
            files = ((loc.path, kuser.POLICY_KEYS), (loc.local, kuser.LOCAL_KEYS))
        elif loc.state == kuser.LEFTOVER:
            # the PM repository's policy file, for the ledger's keys only: no other key (a jail
            # root, an MCP tool) is taken from a config the kit refuses
            files = ((loc.path, LEDGER_KEYS),)
        else:
            files = ((loc.path, tuple(kuser.SCHEMA)),)
        for path, keys in files:
            data, broken = _tolerant_read(path)
            if broken:
                # the rules name loc.path's file; the local file names itself
                out['broken'] = broken if path == loc.path else '%s: %s' % (loc.display(path), broken)
                if set(LEDGER_KEYS) & set(keys):
                    _unread(out, out['broken'])
                return out
            for k in keys:
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    out[k] = v
                elif isinstance(v, list) and v and all(isinstance(x, str) and x.strip() for x in v):
                    out[k] = list(v)
            if set(LEDGER_KEYS) & set(keys):
                _ledger_keys(data, loc.pm, out)
        if loc.error:
            # UNMOVED (the home folder's file) and LEFTOVER (the PM repository's) are read for their
            # ledger keys, so the ledger stays protected, and the config is still broken (every
            # subagent write is denied)
            out['broken'] = loc.error
        out['pm_data'] = loc.pm_data
        return out

    def _bad_ledger_keys(self, out):
        """Under a BAD location, LEDGER_KEYS (no other key) of `<pointer>/.pm-kit`'s policy file
        into `out`, relative ones read against the pointer, when the pointer's value is absolute:
        the guard then holds the ledger that folder names, for main sessions too. It only adds
        held paths; the config stays broken, so every subagent write is still refused. True when
        that file was read (a key in it that does not read sets 'ledger_unread' itself); False when
        the pointer is not absolute or the file is absent or does not read: the ledger is then
        unread."""
        pm = self.pm_repo_pointer()
        if not isinstance(pm, str) or not _placed(pm):
            return False
        path = os.path.join(pm, kuser.DIR_NAME, kuser.FILE_NAME)
        data, why = _tolerant_read(path)
        if why or not os.path.isfile(path):
            return False
        _ledger_keys(data, pm, out)
        return True

    def session_role(self, event):
        return self._role(event, self.environ)

    def host_profile(self):
        if callable(self._profile):              # the hook's adapter, read only when a rule asks
            self._profile = self._profile()
        if self._profile is None:
            from kitlib.host import registry
            self._profile = registry.host(registry.selected(self.environ)).profile
        return self._profile
