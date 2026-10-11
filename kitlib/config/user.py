"""The machine's own kit settings: the machine config, FILE_NAME in the folder DIR_NAME of the
home folder (config_file).

The ledger path is read from this file only: no `.kit.json` key, no ledger environment variable
and no command-line flag sets it, so a repository cannot point the ledger somewhere else. `~` is
os.path.expanduser's: USERPROFILE on Windows, HOME elsewhere, so an environment that changes that
variable changes which machine config is read. An absent file is an empty config; a present file that cannot be read, or that
holds an unknown key or a bad value, is a ConfigError (never silently "no ledger").

The folder moved (docs/kit-json.md "Moving the machine config folder"): locate() picks the file
read. It is DIR_NAME's file when that exists, else the retired folder's
(kitlib.config.migration.OLD_DIR_NAME) while that one exists; the two are never merged. A new
folder or file that cannot be told apart from absent is BAD, a ConfigError, never a fallback.
Writers write DIR_NAME's file only (writable_file).
"""
import collections
import os
import stat
import sys
import tempfile

from .migration import MOVE_STEPS, OLD_DIR_NAME
from .types import ConfigError, Enum, Field, ListOf, MapOf, Obj, Regex, Str, T, is_note, parse_json

# The machine config's folder in the home folder. The one place the kit names it: code derives
# the folder from config_dir() and messages from DISPLAY or display(), so a new name changes this
# line only.
DIR_NAME = '.pm-kit'
FILE_NAME = 'config.json'
# How help text names the machine config file; display() names the file actually read.
DISPLAY = '~/%s/%s' % (DIR_NAME, FILE_NAME)

# locate() states: which file is read.
NEW = 'new'      # DIR_NAME's file; the old folder has none
OLD = 'old'      # the old folder's file: DIR_NAME has none (the fallback)
BOTH = 'both'    # both have one: DIR_NAME's is read, the old one ignored
NONE = 'none'    # neither: an empty config, named at DIR_NAME
BAD = 'bad'      # DIR_NAME's folder or file cannot be read as present or absent: a ConfigError
# The kit's temp folder at the root of a checkout's drive (work_temp).
TEMP_DIR_NAME = 'AgentTemp'


class AbsPath(Str):
    def __init__(self):
        Str.__init__(self, what='an absolute path')

    def check(self, v, where, errs):
        Str.check(self, v, where, errs)
        if isinstance(v, str) and v.strip() and not os.path.isabs(v):
            errs.append('%s must be an absolute path, not %r' % (where, v))

    def describe(self):
        return 'absolute path'


class Owned(T):
    """An object another kit module reads and checks itself; only its being an object is checked here."""

    def __init__(self, module):
        self.module = module

    def check(self, v, where, errs):
        if not isinstance(v, dict):
            errs.append('%s must be an object' % where)

    def describe(self):
        return 'object, checked by %s' % self.module


HOME_PREFIXES = ('~/', '~\\')


class HomeOrAbsPath(Str):
    """An absolute path, or one under the user's home written `~/...` (expand_home)."""

    def __init__(self):
        Str.__init__(self, what='an absolute path or ~/<path>')

    def check(self, v, where, errs):
        Str.check(self, v, where, errs)
        if isinstance(v, str) and v.strip() and not os.path.isabs(v) and not v.startswith(HOME_PREFIXES):
            errs.append('%s must be an absolute path or ~/<path>, not %r' % (where, v))

    def describe(self):
        return 'absolute path or ~/<path>'


def expand_home(path, home):
    """`path` with a leading `~/` (or `~\\`) read as the folder `home`; else unchanged."""
    if isinstance(path, str) and path.startswith(HOME_PREFIXES):
        return os.path.normpath(os.path.join(home, path[2:]))
    return path


# kitlib.sessionmark.RESERVED, repeated here so the machine config (which the lock tool reads)
# does not import the session mark; tests/config/test_user_docs.py keeps the two equal
RESERVED_MARKS = ('owner', 'subagent')


class MarkMap(MapOf):
    """A map from session mark to a value: a reserved mark (owner, subagent) never names a
    session (RESERVED_MARKS), so it is refused."""

    def check(self, v, where, errs):
        MapOf.check(self, v, where, errs)
        if isinstance(v, dict):
            for k in v:
                if isinstance(k, str) and k.strip().lower() in RESERVED_MARKS:
                    errs.append('%s: %r is not a session mark (it is reserved)' % (where, k))


# guard_mark_roots: one more write root of the guard's write jail, for main sessions of one mark
MARK_ROOT = Obj({
    'path': Field(HomeOrAbsPath(), None, 'the folder, absolute or ~/<path>'),
    'mode': Field(Enum('add-only', 'read-only', 'open'), None, 'add-only: new files only, never overwrite, move or delete; read-only: no write; open: any write'),
    'why': Field(Str(), None, 'the reason, a ruling or a measurement'),
}, required=('path', 'mode'), what='{path, mode, why}')

# ledger_refs: where tools/ledger_refs.py looks for citations of a ruling id (docs/ledger.md "ledger_refs").
LEDGER_REFS = Obj({
    'repos': Field(MapOf(Obj({
        'path': Field(AbsPath(), None, 'the repository'),
        'ref': Field(Str(), None, 'the committed ref searched, origin/main say'),
    }, required=('path', 'ref'), what='{path, ref}'), what='name'), {}, 'repositories searched at a ref'),
    'folders': Field(MapOf(AbsPath(), what='name'), {}, 'folders or files searched as they are on disk'),
}, what='object {repos: map name -> {path, ref}, folders: map name -> absolute path}')

# One line per key. A key another module owns is registered as Owned(<that module>), which checks it.
SCHEMA = {
    'ledger_path': Field(AbsPath(), None, 'the rulings ledger file; set here only'),
    'ledger_repos': Field(ListOf(Str(pattern=r'[a-z0-9][a-z0-9-]*', what='a lower-case repo name')), None, 'the repo columns `ledger.py add --repo` writes, besides `all`; unset: any lower-case name (docs/ledger.md)'),
    'pending_path': Field(AbsPath(), None, "the owner's pending-steps file tools/pending.py edits (docs/pending.md)"),
    'ledger_refs': Field(LEDGER_REFS, None, 'where tools/ledger_refs.py looks for citations of a ruling id; unset: the ledger file only (docs/ledger.md "ledger_refs")'),
    'standing_delegation': Field(Str(pattern=r'R-[0-9]{4,}', what='a ruling id R-nnnn'), None, 'the ledger entry that delegates in general; the backlog refuses it as the ruling behind any one change. Unset: no id is refused for that'),
    'scrub_patterns': Field(ListOf(Regex()), None, "case-insensitive patterns for the install's own names (repositories, owner, accounts) that the shipped-text tests and tests/scrub/test_generic.py also refuse"),
    'guard_mcp_prefix': Field(Str(), None, 'the lock MCP tool-name prefix the guard denies owner-only tools under; unset: the kit default (docs/guard.md "Lock MCP")'),
    'guard_mcp_tools': Field(ListOf(Str()), None, 'more full tool names the guard treats as the lock MCP\'s owner-only tools, as a PreToolUse payload names them (docs/guard.md "Lock MCP")'),
    'guard_agent_marks': Field(ListOf(Str()), None, 'the session marks the guard treats as agent sessions for the kill-by-name, lock-state, kit-json and write-jail rules; it replaces the default list, so it names every mark it holds; unset: coordinator, conductor (removed after the switch), deep-worker, consultant and pm (docs/guard.md "Session mark")'),
    'guard_mark_roots': Field(MarkMap(ListOf(MARK_ROOT), what='mark'), None, 'more write-jail roots for the main sessions of one launcher mark, as {mark: [{path, mode, why}]}; a root that is, holds or lies inside either machine config folder, the current or the retired one, is ignored unless it is read-only (docs/guard.md "Write jail")'),
    'pm_data': Field(Owned('kitlib.pmdata'), None, 'the worktree of the PM data branch: {path, branch, remote} (docs/pm-data-branch.md "Layout")'),
    'pm_start': Field(Owned('kitlib.pmstart'), None, 'the PM session-start checklist settings (docs/pm-start.md "Config"); among them hub_urls: account label -> Action Hub URL, one copy per account; hub_url: the single copy, read only while hub_urls has no entry; hub_mirror: the absolute mirror folder for an install without pm_data (docs/action-hub.md "One copy per account")'),
    'coordinator_start': Field(Owned('kitlib.coordinatorstart'), None, 'the coordinator session-start settings'),
    'conductor_start': Field(Owned('kitlib.coordinatorstart'), None, 'the old name of coordinator_start, read when coordinator_start is absent; removed after the switch'),
    'pm_watch': Field(Owned('kitlib.pmwatch'), None, 'the PM watcher settings'),
    'launcher': Field(Owned('kitlib.launcher'), None, 'the session launcher settings: state folder, repos, account folders (docs/launcher.md "Settings")'),
    'backlog_lint': Field(Owned('kitlib.backloglint'), None, 'the backlog lint repositories'),
    'machine_lock': Field(Owned('kitlib.lock.site'), None, 'the machine lock settings: build daemons at release, lock-window notices'),
    'temp_dir': Field(AbsPath(), None, "the folder the kit's own temp files go in (test run folders, gate logs); unset: `AgentTemp` at the root of the checkout's drive, or the shared temp folder on its drive (docs/test-tools.md \"Temp folders\"); older kit copies reject the key"),
    'agent_temp_sweep': Field(Owned('optional/agenttemp/agent_temp_sweep.py'), None, "the agents' temp folder to sweep (`dir`), the age past which an unused entry goes (`max_age_days`, default 3) and the entries it never removes (`keep`, relative paths such as `claude/<project>/<session>`); `tools/machine_setup.py` adds it when absent (docs/machine-setup.md \"Extras\")"),
    'plugin': Field(Owned('kitlib.pluginsource'), None, 'where the plugin export of pushed kit main lives, and whether pm_watch refreshes it on its own (docs/plugin-source.md); older kit copies reject the key; set it only after every folder is migrated'),
}


class HomeFenced(BaseException):
    """home_dir() resolved a real home folder in a fenced process (the kit's tests and their
    children): one fence_home() fenced, or one whose environment carries FENCE_ENV. A
    BaseException, so the `except Exception` that isolates a check (pmstart's "could not check"
    lines, pmwatch's third failure) cannot turn the refusal into a line a test may miss."""


FENCE_MESSAGE = ('home lookup refused under the kit\'s tests: it resolved the real home folder; '
                 'give it an explicit home or folders (a temp folder), or patch USERPROFILE/HOME '
                 'to one (docs/test-tools.md "Home fence")')
UNPROVEN_MESSAGE = ('home lookup refused under the kit\'s tests: Windows did not name the profile '
                    'folder (%s), so no home can be shown to be a temp folder (docs/test-tools.md '
                    '"Home fence")')
# The fence's marker: the home folder the kit's test run started with, in the environment of the
# test units (tools/kit_tests.py run_pass) and of a process that imports the tests package
# (tests/__init__.py), so the tools a test starts inherit it. Set by fence_env only; read by
# home_dir() only, at each call. Unset or empty: no marker. A real run never has it.
FENCE_ENV = 'KIT_HOME_FENCE'
# The real home folder (_home_key) recorded by fence_home(); None: fence_home() was not called.
_FENCED_HOME = None
# (real_profile()'s folder, its _real_key) once Windows was asked; None before.
_PROFILE = None


def _home_key(path):
    """The key two spellings of one folder path share: absolute, normalised, case folded, and
    without the extended-length prefix (`\\\\?\\`, or `\\\\?\\UNC\\` before a share), which
    os.path.abspath and os.path.realpath both keep. A share that names a folder of this machine
    keeps its own key: nothing here resolves it."""
    path = os.path.abspath(path)
    if path.startswith('\\\\?\\'):
        rest = path[4:]
        path = '\\\\' + rest[4:] if rest[:4].lower() == 'unc\\' else rest
    return os.path.normcase(os.path.normpath(path))


def _real_key(path):
    """_home_key of the folder `path` really is (os.path.realpath), so a junction, a symlink or a
    short (8.3) name of a folder has that folder's key. A path that is not there keeps its
    spelling."""
    try:
        return _home_key(os.path.realpath(path))
    except (OSError, ValueError):
        return _home_key(path)


def _windows_profile():
    """The user's profile folder as Windows reports it (SHGetKnownFolderPath, FOLDERID_Profile);
    OSError when it does not. kitlib.lock.store.profile_dir is the same lookup."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [('d1', wintypes.DWORD), ('d2', wintypes.WORD), ('d3', wintypes.WORD),
                    ('d4', ctypes.c_ubyte * 8)]
    folder = GUID(0x5E6C858F, 0x0E22, 0x4760,
                  (ctypes.c_ubyte * 8)(0x9A, 0xFE, 0xEA, 0x33, 0x17, 0xB6, 0x71, 0x73))
    get = ctypes.WinDLL('shell32').SHGetKnownFolderPath
    get.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                    ctypes.POINTER(ctypes.c_wchar_p)]
    get.restype = ctypes.c_long
    free = ctypes.WinDLL('ole32').CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    path = ctypes.c_wchar_p()
    hr = get(ctypes.byref(folder), 0, None, ctypes.byref(path))
    try:
        if hr != 0 or not path.value:
            raise OSError('SHGetKnownFolderPath(FOLDERID_Profile) failed: 0x%08X' % (hr & 0xFFFFFFFF))
        return path.value
    finally:
        free(path)


def _profile():
    global _PROFILE
    if _PROFILE is None:
        path = _windows_profile()
        _PROFILE = (path, _real_key(path))
    return _PROFILE


def real_profile():
    """The user's profile folder from Windows, never from the environment: no USERPROFILE or HOME
    moves it. Asked once per process. It names the folder and reads nothing in it; the fence
    compares against it, so it is not itself fenced. OSError when Windows does not answer."""
    return _profile()[0]


def fence_env(env):
    """Mark the environment mapping `env` for the home fence, and return it: FENCE_ENV becomes the
    home folder this process resolves now, unless `env` already carries a marker (an outer test
    run's home is kept, so a nested run under a temp home never records the temp home as real) or
    the home does not expand to an absolute path. A path computation: nothing is read."""
    if not env.get(FENCE_ENV):
        h = os.path.expanduser('~')
        if os.path.isabs(h):
            env[FENCE_ENV] = os.path.abspath(h)
    return env


def fence_home():
    """Fence this process's home lookups: record the home folder resolved now, after which
    home_dir() raises HomeFenced whenever it resolves to that folder again. tests/__init__.py calls
    it once, before any test patches the environment; kitlib never does, and a real run carries no
    FENCE_ENV either, so a real run is unfenced. Returns the recorded key."""
    global _FENCED_HOME
    _FENCED_HOME = _home_key(os.path.expanduser('~'))
    return _FENCED_HOME


def _refused(h, marker):
    """Whether the home `h` is a real one: the folder fence_home() recorded, the one `marker`
    names, or the profile folder Windows reports, each compared as spelled and as the folder it
    really is (_real_key). HomeFenced when Windows does not answer: without its profile folder no
    home is shown to be a temp one."""
    try:
        profile, profile_real = _profile()
    except Exception as e:
        raise HomeFenced(UNPROVEN_MESSAGE % e)
    keys = {_home_key(profile), profile_real}
    for path in (_FENCED_HOME, marker):
        if path and _home_key(path) not in keys:
            keys.update((_home_key(path), _real_key(path)))
    return _home_key(h) in keys or _real_key(h) in keys


def home_dir():
    """The user's home folder (os.path.expanduser: USERPROFILE on Windows, HOME elsewhere).
    HomeFenced in a fenced process (fence_home, or FENCE_ENV in the environment) when it resolves
    to a real home (_refused): a test gives an explicit home, or points USERPROFILE/HOME at a temp
    folder, for itself and for the tools it starts. An unfenced process gets the folder as ever:
    one environment read more, and Windows is not asked."""
    h = os.path.expanduser('~')
    marker = os.environ.get(FENCE_ENV)
    if _FENCED_HOME is None and not marker:
        return h
    if _refused(h, marker):
        raise HomeFenced(FENCE_MESSAGE)
    return h


def shared_temp(environ=None, platform=None):
    """The temp folder every process on the machine shares, whatever TEMP/TMP it inherited.
    Windows: %LOCALAPPDATA%/Temp, and a RuntimeError when LOCALAPPDATA is
    unset or empty: falling back to TEMP would split logs between processes. Elsewhere there is
    one TEMP, so tempfile.gettempdir() is kept."""
    env = os.environ if environ is None else environ
    if (sys.platform if platform is None else platform) != 'win32':
        return tempfile.gettempdir()
    lad = env.get('LOCALAPPDATA')
    if not lad:
        raise RuntimeError('LOCALAPPDATA is unset or empty: the shared temp folder '
                           '(%LOCALAPPDATA%/Temp) cannot be resolved, and following TEMP instead '
                           'would split logs between processes')
    return os.path.join(lad, 'Temp')


def _drive(path):
    return os.path.normcase(os.path.splitdrive(os.path.abspath(path))[0])


def work_temp(near, home=None, environ=None, platform=None):
    """The folder the kit's own temp files for the checkout `near` go in (test run folders and
    their fixture repositories, gate logs), so they stay on the checkout's drive (a Dev Drive,
    say) instead of the system drive. In order:
    - the machine config's `temp_dir` when set;
    - on Windows, TEMP_DIR_NAME at the root of the drive (or UNC share) `near` is on, when that
      root exists and is not the drive the shared temp folder is on;
    - else shared_temp(): no drive-root folder where the user's temp folder serves already.
    It reads `near`, the machine config and LOCALAPPDATA, never TEMP or TMP, so a gate writing
    logs and a reader in another shell agree. It makes nothing: a caller that cannot make its
    folder here falls back to shared_temp() (gate logs) or its own temp folder (make_temp).
    RuntimeError: no LOCALAPPDATA and no usable drive root; ConfigError: a bad machine config."""
    configured = user_config(home)['temp_dir']
    if configured:
        return configured
    plat = sys.platform if platform is None else platform
    if plat != 'win32':
        return shared_temp(environ, plat)
    try:
        shared = shared_temp(environ, plat)
    except RuntimeError:
        shared = None
    drive = os.path.splitdrive(os.path.abspath(near))[0]
    if drive and os.path.isdir(drive + os.sep) and (shared is None
                                                    or _drive(near) != _drive(shared)):
        return os.path.join(drive + os.sep, TEMP_DIR_NAME)
    if shared is None:
        return shared_temp(environ, plat)       # raises, saying why
    return shared


def make_temp(near, prefix, home=None):
    """(folder, note): a new folder (tempfile.mkdtemp, `prefix`) in work_temp(near), that parent
    made if missing; else, with a note saying why, one in this process's temp folder."""
    parent = None
    try:
        parent = work_temp(near, home)
        os.makedirs(parent, exist_ok=True)
        return tempfile.mkdtemp(prefix=prefix, dir=parent), None
    except (OSError, RuntimeError, ConfigError) as e:
        folder = tempfile.mkdtemp(prefix=prefix)
        return folder, '%s is in this process\'s temp folder: %s could not be used (%s)' % (
            folder, parent or 'the kit temp folder', e)


class Location(collections.namedtuple('Location', 'state path new old error home')):
    """Which machine config file is read (locate): `state` one of NEW, OLD, BOTH, NONE, BAD;
    `path` the file read (DIR_NAME's for NONE and BAD); `new` and `old` the two candidate files;
    `error` why a BAD location cannot be read, else None; `home` the home folder."""

    def display(self, path=None):
        """`path` (default the file read) as ~/<folder>/<file>."""
        p = path or self.path
        return '~/%s/%s' % (os.path.basename(os.path.dirname(p)), os.path.basename(p))


def _kind(path):
    """'absent', 'dir', 'file' or 'other' (a dangling link, a device) for `path`, links followed.
    An OSError other than not-found propagates: a path that cannot be examined is not absent."""
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        try:
            os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return 'absent'
        return 'other'
    if stat.S_ISDIR(st.st_mode):
        return 'dir'
    return 'file' if stat.S_ISREG(st.st_mode) else 'other'


def locate(home=None):
    """The Location of the machine config under `home` (default the user's). It decides on
    DIR_NAME's config file, never on its folder alone, so another writer making the folder first
    does not hide the old file. It reads nothing but the file system's answers and never raises."""
    h = home or home_dir()
    new_dir, old_dir = os.path.join(h, DIR_NAME), os.path.join(h, OLD_DIR_NAME)
    new, old = os.path.join(new_dir, FILE_NAME), os.path.join(old_dir, FILE_NAME)

    def bad(why):
        return Location(BAD, new, new, old, why, h)
    try:
        d = _kind(new_dir)
        if d not in ('absent', 'dir'):
            return bad('%s is not a folder: the machine config cannot be read' % new_dir)
        n = _kind(new) if d == 'dir' else 'absent'
    except OSError as e:
        return bad('cannot tell whether the machine config %s exists: %s' % (new, e))
    if n not in ('absent', 'file'):
        return bad('%s is not a file: the machine config cannot be read' % new)
    try:
        o = _kind(old)
    except OSError as e:
        if n == 'file':
            return Location(NEW, new, new, old, None, h)
        return bad('cannot tell whether the machine config %s exists: %s' % (old, e))
    if n == 'file':
        return Location(NEW if o == 'absent' else BOTH, new, new, old, None, h)
    if o == 'absent':
        return Location(NONE, new, new, old, None, h)
    return Location(OLD, old, new, old, None, h)


def config_dir(home=None):
    """The folder of the machine config file read under `home` (default the user's): locate()."""
    return os.path.dirname(locate(home).path)


def config_file(home=None):
    """The machine config file read (locate()); for a BAD location the new file. Never raises."""
    return locate(home).path


def readable_file(home=None):
    """The machine config file a reader opens (locate()). ConfigError when the location is BAD:
    a reader that opens the file itself (pmstart, pmwatch, backloglint, ...) must not take a new
    file it cannot examine for an absent one."""
    loc = locate(home)
    if loc.error:
        raise ConfigError(loc.error)
    return loc.path


def display(home=None):
    """The machine config file read, as messages name it: ~/<folder>/config.json."""
    return locate(home).display()


def writable_file(home=None):
    """The file a writer of the machine config writes: DIR_NAME's, always. ConfigError when the
    location is BAD, or when only the old folder has a config (OLD): a new file holding one key
    would hide every other key, so the owner moves the file first (migration.MOVE_STEPS)."""
    loc = locate(home)
    if loc.error:
        raise ConfigError(loc.error)
    if loc.state == OLD:
        raise ConfigError(MOVE_STEPS % (loc.old, loc.new))
    return loc.new


def user_config(home=None, location=None):
    """The checked machine config, every schema key present (None where unset). `location` is a
    Location already taken (the guard takes one per call), else locate(home)."""
    loc = location or locate(home)
    if loc.error:
        raise ConfigError(loc.error)
    path = loc.path
    try:
        with open(path, encoding='utf-8-sig') as f:
            text = f.read()
    except FileNotFoundError:
        text = None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ConfigError('cannot read %s: %s' % (path, e))
    data = {}
    if text is not None:
        data = parse_json(text, path)
        errs = []
        Obj(SCHEMA, what='machine config').check(data, path, errs)
        if errs:
            raise ConfigError('; '.join(errs))
    out = {k: f.default for k, f in SCHEMA.items()}
    out.update({k: v for k, v in data.items() if k in SCHEMA})
    return out


def failing_keys(data):
    """The sorted top-level keys of the machine config object `data` that user_config refuses:
    unknown, or holding a value its type refuses. Names only: no value is returned, so a caller
    may print them where the value must not appear (kitlib.preflight.configline)."""
    out = []
    for k, v in data.items():
        if is_note(k):
            continue
        f = SCHEMA.get(k)
        errs = []
        if f is None:
            errs.append(k)
        elif not (v is None and f.default is None):
            f.type.check(v, k, errs)
        if errs:
            out.append(k)
    return sorted(out)


def ledger_path(home=None):
    """The ledger path, or None when the machine config does not set one."""
    return user_config(home)['ledger_path']


def ledger_repos(home=None):
    """The repo columns the ledger writer accepts besides `all`, or None (any lower-case name)."""
    return user_config(home)['ledger_repos']


def standing_delegation(home=None):
    """The standing delegation's ruling id, or None when the machine config does not set one."""
    return user_config(home)['standing_delegation']


def doc_lines():
    return ['- `%s` (%s): %s' % (k, f.type.describe(), f.doc) for k, f in SCHEMA.items()]
