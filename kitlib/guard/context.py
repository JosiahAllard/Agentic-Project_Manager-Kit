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
  gitread            kitlib.guard.gitread (alias, has_commit, is_shallow), or a test's fake
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
                     sha's history and hash the gate config that commit holds
  user()             the machine config (ledger_path and the guard_* keys), read
                     tolerantly: a file the strict reader refuses only for keys this kit does not
                     know still yields its absolute ledger_path and its string or string-list
                     values. A file that cannot be read, is
                     not a JSON object, or whose ledger_path is not an absolute path string sets
                     'broken' (why), and the ledger rule then denies every subagent write. A
                     missing file is an empty config, not a broken one. The file is the one
                     config_location() names; a BAD location sets 'broken' too (fail closed).
                     A file that does not parse (not JSON, or not an object) is read through
                     its last good copy (last_good) when there is one: every key the bad save
                     left alone keeps the copy's value, a changed key reads as unset, and a
                     change to a key in WHOLE keeps 'broken'.
  last_good()        the LastGood copy standing in for a machine config that does not parse, or
                     None. The copy is LAST_GOOD beside the file read, written here alone
                     (keep_last_good, each time the strict reader takes a file the copy does not
                     match) and refused to agents like the file itself (jail.config_dirs). The
                     keys a bad save changed are changed_keys of the two texts.
  stand_in()         the sentence a refusal adds while the copy stands in, else None.
  config_location()  kitlib.config.user.locate(home), taken once per Ctx, so every read of
                     the machine config in one hook call reads the same file.
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
kitlib.pmdata.machine_settings's answer, or raising its errors).
"""
import collections
import difflib
import json
import os
import re
import tempfile

from kitlib import config as kconfig
from kitlib import kitroot
from kitlib import pmdata
from kitlib import sessionmark
from kitlib.config import user as kuser
from kitlib.git import refs, repo
from kitlib.guard import gitread as fsgitread
from kitlib.guard import paths
from kitlib.guard import place as fsplace

ROLE_ENV = sessionmark.ENV
OWNER = 'owner'
SUBAGENT = 'subagent'


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


# ---------------------------------------------------------------- the machine config's last good copy

LAST_GOOD = 'config.last-good.json'
# Keys a guard rule reads where an unset value would loosen the rule (the ledger, the lock MCP's
# owner-only tools, the agent marks, the hub's store and ports): a bad save that changes one keeps
# the whole machine config broken. Any other changed key reads as unset: guard_mark_roots then adds
# no root, pm_data stays unreadable (pm_data_error), and the guard reads none of the rest.
WHOLE = ('ledger_path', 'guard_mcp_prefix', 'guard_mcp_tools', 'guard_agent_marks', 'hub')
MAX_TEXT = 1 << 20                              # a larger file is not diffed: no stand-in
_WS = re.compile(r'[ \t\n\r]*')
_DECODER = json.JSONDecoder()


class LastGood(collections.namedtuple('LastGood', 'path values changed why')):
    """The last good copy standing in for a machine config that does not parse: `path` the copy,
    `values` its strict read (kitlib.config.user.user_config), `changed` the keys the bad save
    changed (changed_keys), `why` why the file read does not parse."""


def last_good_path(location):
    """The last good copy of the machine config file the kitlib.config.user.Location reads."""
    return os.path.join(os.path.dirname(location.path), LAST_GOOD)


def _read(path):
    """The text of `path` (utf-8, a BOM dropped), or None when it does not exist."""
    try:
        with open(path, encoding='utf-8-sig') as f:
            return f.read()
    except FileNotFoundError:
        return None


def keep_last_good(home, location):
    """Keep the machine config file `location` reads as its last good copy (last_good_path) when
    the copy differs: the text is written to a temporary file beside it, read back by the strict
    reader, and moved over the copy. Best effort: any failure leaves the old copy as it was."""
    if location.error:
        return
    dest = last_good_path(location)
    try:
        text = _read(location.path)
        if text is None or len(text) > MAX_TEXT or text == _read(dest):
            return
        fd, tmp = tempfile.mkstemp(prefix=LAST_GOOD + '.', suffix='.tmp', dir=os.path.dirname(dest))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
                f.write(text)
            kuser.user_config(home, location=location._replace(path=tmp))
            os.replace(tmp, dest)
            tmp = None
        finally:
            if tmp:
                os.remove(tmp)
    except (OSError, ValueError, kconfig.ConfigError):
        pass


def _members(text):
    """[(key, start, end)] for each top-level member of the JSON object `text`, from its key's
    opening quote to its value's end; None when `text` is no JSON object."""
    try:
        i = _WS.match(text, 0).end()
        if text[i:i + 1] != '{':
            return None
        i = _WS.match(text, i + 1).end()
        out = []
        if text[i:i + 1] == '}':
            return out
        while text[i:i + 1] == '"':
            key, j = json.decoder.scanstring(text, i + 1)
            j = _WS.match(text, j).end()
            if text[j:j + 1] != ':':
                return None
            _value, end = _DECODER.raw_decode(text, _WS.match(text, j + 1).end())
            out.append((key, i, end))
            k = _WS.match(text, end).end()
            if text[k:k + 1] == '}':
                return out
            if text[k:k + 1] != ',':
                return None
            i = _WS.match(text, k + 1).end()
        return None
    except (ValueError, RecursionError):
        return None


def changed_keys(good, bad, names):
    """The top-level keys a save turning the text `good` (a JSON object) into `bad` changed, by
    lines: a member of `good` with a removed or replaced line inside it, or a line inserted inside
    it, and every name of `names` or of `good`'s keys written, quoted or not, in a line `bad` adds.
    None when `good` is no JSON object or either text is larger than MAX_TEXT."""
    members = _members(good) if len(good) <= MAX_TEXT and len(bad) <= MAX_TEXT else None
    if members is None:
        return None
    old, new = good.splitlines(True), bad.splitlines(True)
    at = [0]
    for line in old:
        at.append(at[-1] + len(line))
    words = set(names) | {k for k, _s, _e in members}
    changed = set()
    ops = difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    for op, i1, i2, j1, j2 in ops:
        if op == 'equal':
            continue
        a, b = at[i1], at[i2]
        for key, start, end in members:
            if (start < b and a < end) if i1 < i2 else start < a < end:
                changed.add(key)
        added = ''.join(new[j1:j2])
        changed.update(w for w in words if re.search(r'(?<![\w-])%s(?![\w-])' % re.escape(w), added))
    return changed


def _machine_pm_data(home, location=None):
    """kitlib.pmdata.machine_settings(home). When the strict reader refuses the machine config,
    its pm_data key alone is read and checked with the same rules, so another key's fault does
    not hide the PM data worktree; a file that cannot be read or parsed, or a BAD `location`
    (kitlib.config.user.locate), raises, and the guard fails closed."""
    loc = location or kuser.locate(home)
    try:
        return pmdata.machine_settings(home, location=loc)
    except kconfig.ConfigError as e:
        if loc.error:
            raise
        path = loc.path
        try:
            with open(path, encoding='utf-8-sig') as f:
                data = json.load(f)
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            raise e
        if not isinstance(data, dict):
            raise e
        return pmdata.check_settings(data.get(pmdata.KEY), '%s key %s' % (path, pmdata.KEY))


def _same_dir(a, b):
    return repo.real(a) == repo.real(b) or repo.same_path(a, b)


class Ctx(object):
    def __init__(self, environ=None, home=None, store=None, classify=None, load=None,
                 gate_ok=None, git=None, lock_held=None, role_resolver=None, tempdir=None,
                 pm_data=None, gitread=None, kit=None, profile=None):
        self.environ = dict(os.environ if environ is None else environ)
        self.home = home or kuser.home_dir()
        self.kit = kit or kitroot.root()
        self.store = store
        self._classify = classify or fsplace.classify
        self._load = load or (lambda top: kconfig.load(top, home=self.home, machine=self.user))
        self._gate_ok = gate_ok
        self.git = git or refs
        self.gitread = gitread or fsgitread
        self._lock_held = lock_held
        self._role = role_resolver or default_session_role
        self._profile = profile
        self.tempdir = tempdir or self.environ.get('TEMP') or tempfile.gettempdir()
        self._places, self._configs, self._config_errors = {}, {}, {}
        self._user = None
        self._location = None
        self._pm_data = pm_data or self._machine_pm_data
        self._last_good = None                   # (LastGood or None,), read once
        self._pm_settings = None                 # (settings or None, error or None), read once
        self._pm_records = {}                    # paths.norm(common) -> (Record or None, error or None)
        self._pm_places = {}                     # key of a place answered as the PM data worktree -> its branch
        self._pm_place_errors = {}               # key -> why the place could not be told

    # ------------------------------------------------------------ places and config

    def place(self, path):
        if not path:
            return repo.Place(repo.UNKNOWN, None, 'no path', None, None)
        key = paths.norm(path, absolute=True)
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
            self._location = kuser.locate(self.home)
        return self._location

    def user(self):
        if self._user is None:
            try:
                self._user = kuser.user_config(self.home, location=self.config_location())
            except kconfig.ConfigError as e:
                self._user = self._tolerant(str(e))
            else:
                keep_last_good(self.home, self.config_location())
        return self._user

    def last_good(self):
        """The LastGood copy standing in for the machine config, read once; None when the file
        parses, cannot be read or is absent, or when there is no copy the strict reader takes."""
        if self._last_good is None:
            self._last_good = (self._read_last_good(),)
        return self._last_good[0]

    def _read_last_good(self):
        loc = self.config_location()
        if loc.error:
            return None
        try:
            text = _read(loc.path)
        except (OSError, ValueError):
            return None
        if text is None:
            return None
        try:
            data = json.loads(text)
        except (ValueError, RecursionError) as e:
            why = 'it is not valid JSON: %s' % e
        else:
            if isinstance(data, dict):
                return None
            why = 'it must be an object, not %s' % type(data).__name__
        copy = last_good_path(loc)
        try:
            good = _read(copy)
            if good is None:
                return None
            values = kuser.user_config(self.home, location=loc._replace(path=copy))
            if _read(copy) != good:             # replaced while it was read
                return None
        except (OSError, ValueError, kconfig.ConfigError):
            return None
        changed = changed_keys(good, text, set(kuser.SCHEMA))
        if changed is None:
            return None
        return LastGood(copy, values, frozenset(changed), why)

    def stand_in(self):
        """The sentence a refusal adds while the last good copy stands in for keys of a machine
        config that does not parse; None when it does not (a change to a key in WHOLE keeps the
        config broken, and the ledger rule's own message names the copy)."""
        lg = self.last_good()
        if lg is None or any(k in lg.changed for k in WHOLE):
            return None
        loc = self.config_location()
        return ('The machine config %s does not parse (%s); the guard reads its last good copy %s '
                'for every key the bad save left alone (changed: %s).' % (
                    loc.display(), lg.why, loc.display(lg.path), ', '.join(sorted(lg.changed)) or 'none'))

    def _machine_pm_data(self):
        """_machine_pm_data of the machine config, its pm_data read from the last good copy while
        that stands in and the bad save left pm_data alone."""
        loc = self.config_location()
        try:
            return _machine_pm_data(self.home, loc)
        except kconfig.ConfigError as e:
            lg = self.last_good()
            if lg is None:
                raise
            if pmdata.KEY in lg.changed:
                raise kconfig.ConfigError('%s; its last good copy %s does not stand in for %s, which '
                                          'the bad save changed' % (e, loc.display(lg.path), pmdata.KEY))
            return pmdata.check_settings(lg.values.get(pmdata.KEY), '%s key %s' % (lg.path, pmdata.KEY))

    def _from_copy(self, out, why):
        """`out` (every key unset, 'error' set) filled from the last good copy for the keys the
        bad save left alone; 'broken' (`why`) when there is no copy or the save changed a key in
        WHOLE."""
        lg = self.last_good()
        if lg is None:
            out['broken'] = why
            return out
        whole = [k for k in WHOLE if k in lg.changed]
        if whole:
            out['broken'] = '%s, and its last good copy %s does not stand in for %s, which the bad save changed' % (
                why, self.config_location().display(lg.path), ', '.join(whole))
            return out
        for k in kuser.SCHEMA:
            if k not in lg.changed:
                out[k] = lg.values.get(k)
        return out

    def _tolerant(self, why):
        out = {k: None for k in kuser.SCHEMA}
        out['error'] = why
        loc = self.config_location()
        if loc.error:
            out['broken'] = loc.error
            return out
        path = loc.path
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
            return self._from_copy(out, 'it is not valid JSON: %s' % e)
        if not isinstance(data, dict):
            return self._from_copy(out, 'it must be an object, not %s' % type(data).__name__)
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

    def host_profile(self):
        if callable(self._profile):              # the hook's adapter, read only when a rule asks
            self._profile = self._profile()
        if self._profile is None:
            from kitlib.host import registry
            self._profile = registry.host(registry.selected(self.environ)).profile
        return self._profile
