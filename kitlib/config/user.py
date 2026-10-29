"""The machine's own kit settings: the machine config, read from the PM repository the PM-repo
pointer names (kitlib.config.pointer; docs/kit-json.md "Machine config").

With a good pointer (locate OK) the config is two files in `<PM repo>/.pm-kit`: FILE_NAME, tracked,
holds the policy keys (POLICY_KEYS), and LOCAL_FILE_NAME, git-ignored, the machine-local and
personal keys (LOCAL_KEYS); a key in the other file, or one the move drops (DROPPED_KEYS), is a
ConfigError (except a RETIRED key, which is ignored). `pm_data` is never read from a file: it is derived from the pointer and its
repository's kit-pm-data.json record. RELATIVE_KEYS paths written relative are read against the PM
repository. Either file absent is empty.

The ledger path is read from this config only: no `.kit.json` key, no ledger environment variable
and no command-line flag sets it, so a repository cannot point the ledger somewhere else. A
present file that cannot be read, or that holds an unknown key or a bad value, is a ConfigError
(never silently "no ledger").

No pointer and no machine config left in the home folder (NONE) is an empty config. A machine
config still in the home folder (DIR_NAME's, or the retired kitlib.config.migration.OLD_DIR_NAME's)
while the PM repository has none is UNMOVED; one beside the PM repository's files is LEFTOVER (the
config moved: the copy must go, LEFTOVER_WHY); a pointer that is BAD, or a PM folder or file that
cannot be told apart from absent, is BAD: each a ConfigError, never a fallback. home_location()
still reads the home folder's file, for the move (kitlib.config.move) alone.
"""
import collections
import os
import stat
import tempfile

import kitlib.platform
from kitlib.platform.linux import paths as linux_paths

from .migration import OLD_DIR_NAME
from .types import ConfigError, Email, Enum, Field, ListOf, MapOf, Obj, Regex, Str, T, is_note, parse_json

# The machine config's folder in the home folder. The one place the kit names it: code derives
# the folder from config_dir() (None unless the PM repository holds the config), keep_clear_dirs()
# and messages from display(), so a new name changes this line only.
DIR_NAME = '.pm-kit'
FILE_NAME = 'config.json'

# The PM repository's git-ignored machine config file, beside FILE_NAME (LOCAL_KEYS there).
LOCAL_FILE_NAME = 'local.json'

# locate() states: which machine config is read.
OK = 'ok'            # the PM-repo pointer is good: <PM repo>/.pm-kit's FILE_NAME and LOCAL_FILE_NAME
NONE = 'none'        # no pointer and no machine config in the home folder: an empty config
UNMOVED = 'unmoved'  # a machine config still in the home folder, none in the PM repository: a ConfigError
LEFTOVER = 'leftover'  # the PM repository's config, and a copy still in the home folder: a ConfigError
BAD = 'bad'          # the pointer is BAD, or a PM folder or file cannot be read as present or absent
# kitlib.config.pointer's states, repeated (that module imports this one).
OK_POINTER, BAD_POINTER = 'OK', 'BAD'
# home_location() states, the move's source (kitlib.config.move), with NONE and BAD above.
NEW = 'new'      # DIR_NAME's file; the old folder has none
OLD = 'old'      # the old folder's file: DIR_NAME has none
BOTH = 'both'    # both have one: DIR_NAME's is read, the old one ignored
# The kit's temp folder at the root of a checkout's drive (work_temp): the kit's default, which
# the machine config's `temp_dir` overrides.
TEMP_DIR_NAME = 'kit-temp'
# The kit's temp folder when TEMP_DIR_NAME (or the configured `temp_dir`) is a session's TEMP
# (work_temp): at the drive root, or beside the system temp folder.
WORK_DIR_NAME = 'kit-work'
# The user environment values (HKCU\Environment) that name a folder sessions use as TEMP
# (session_temps): the agents' temp folder the launcher points TEMP at, and the desktop's own.
SESSION_VARS = ('AGENT_TEMP', 'TEMP', 'TMP')


class AbsPath(Str):
    def __init__(self):
        Str.__init__(self, what='an absolute path')

    def check(self, v, where, errs):
        Str.check(self, v, where, errs)
        if isinstance(v, str) and v.strip() and not os.path.isabs(v):
            errs.append('%s must be an absolute path, not %r' % (where, v))

    def describe(self):
        return 'absolute path'


class PmRepoPath(AbsPath):
    """A RELATIVE_KEYS path: absolute, or written relative to the PM repository (resolved on read)."""

    def describe(self):
        return 'absolute path, or relative to the PM repo'


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

# Keys the machine config no longer has. A file written before the removal still loads: the key
# is dropped whatever it holds, never refused and never returned. An unknown key is still refused.
RETIRED = ('ledger_refs', 'standing_delegation', 'daily_red_ruling', 'backlog_lint')

# One line per key. A key another module owns is registered as Owned(<that module>), which checks it.
SCHEMA = {
    'owner_email': Field(Email(), None, 'the author email of the owner\'s own commits, for every repository whose .kit.json sets no "owner".email; unset there too: no commit is provable as the owner\'s, so every owner-only change is refused (docs/kit-json.md "With no file"); older kit copies reject the key'),
    'ledger_dir': Field(PmRepoPath(), None, 'the rulings ledger folder: one file `<ledger_dir>/<repo>.md` per `ledger_repos` name, which it needs; set here only (docs/ledger.md "Files"); older kit copies reject the key; set it only after every folder runs a kit that knows it'),
    'ledger_repos': Field(ListOf(Str(pattern=r'[a-z0-9][a-z0-9-]*', what='a lower-case repo name')), None, 'the repos `ledger.py add --repo` writes; `pm` is the machine-wide one; a repository\'s name is its folder name lower-cased with `_` as `-` (`My_Tools-Repo` is `my-tools-repo`); with `ledger_dir`, each name once, and never `all`, `standing` or the frozen file\'s name; without it, the one-file ledger\'s repo columns besides `all`, and unset: any lower-case name (docs/ledger.md)'),
    'ledger_path': Field(PmRepoPath(), None, 'the one-file rulings ledger (R-nnnn ids); set here only; once `ledger_dir` is set it is frozen: read, never written; removed one release after the split'),
    'pending_path': Field(PmRepoPath(), None, "the owner's pending-steps file tools/pending.py edits (docs/pending.md)"),
    'scrub_patterns': Field(ListOf(Regex()), None, "case-insensitive patterns for the install's own names (repositories, owner, accounts) that the shipped-text tests and tests/scrub/test_generic.py also refuse"),
    'guard_mcp_prefix': Field(Str(), None, 'the lock MCP tool-name prefix the guard denies owner-only tools under; unset: the kit default (docs/guard.md "Lock MCP")'),
    'guard_mcp_tools': Field(ListOf(Str()), None, 'more full tool names the guard treats as the lock MCP\'s owner-only tools, as a PreToolUse payload names them (docs/guard.md "Lock MCP")'),
    'guard_agent_marks': Field(ListOf(Str()), None, 'the session marks the guard treats as agent sessions for the kill-by-name, lock-state, kit-json, write-jail and git-stash-shared rules; it replaces the default list, so it names every mark it holds; unset: coordinator, deep-worker, consultant and pm (docs/guard.md "Session mark")'),
    'guard_mark_roots': Field(MarkMap(ListOf(MARK_ROOT), what='mark'), None, 'more write-jail roots for the main sessions of one launcher mark, as {mark: [{path, mode, why}]}; a root that is, holds or lies inside either machine config folder, the current or the retired one, is ignored unless it is read-only (docs/guard.md "Write jail")'),
    'guard_rules': Field(MapOf(Enum('deny', 'warn', 'off'), what='rule'), None, 'the guard\'s rule modes, {rule: deny|warn|off}, over the kit defaults (kitlib.guard.modes.DEFAULTS); set here only, never in .kit.json (docs/guard.md "Rule modes"); older kit copies reject the key; set it only after every folder runs a kit that knows it'),
    'pm_data': Field(Owned('kitlib.pmdata'), None, 'the worktree of the PM data branch: {path, branch, remote} (docs/pm-data-branch.md "Layout")'),
    'hub': Field(Owned('kitlib.hubsite.settings'), None, 'the Action Hub site settings: port, tunnel_port, store, commit_every_s, push (unset: on only when store is unset and pm_data is set), backlog_refresh_s, access: {team, aud, emails}; unset: every default, and the site still runs; older kit copies reject the key; set it only after every folder runs a kit that knows it'),
    'pm_start': Field(Owned('kitlib.pmstart'), None, 'the PM session-start checklist settings (docs/pm-start.md "Config")'),
    'coordinator_start': Field(Owned('kitlib.coordinatorstart'), None, 'the coordinator session-start settings'),
    'pm_watch': Field(Owned('kitlib.pmwatch'), None, 'the PM watcher settings'),
    'launcher': Field(Owned('kitlib.launcher'), None, 'the session launcher settings: state folder, repos, account folders (docs/launcher.md "Settings")'),
    'machine_lock': Field(Owned('kitlib.lock.site'), None, 'the machine lock settings: build daemons at release, lock-window notices'),
    'temp_dir': Field(AbsPath(), None, "the folder the kit's own temp files go in (test run folders, gate logs), and the shared temp folder (gate fallback logs, the worktree sweep log, private index folders, comfy_gen's GPU self-lock and state) while its drive is there, made if missing; on Windows, as the test and gate folder, it is skipped when it is, holds or sits inside a session temp (`agent_temp_sweep` `dir`, or the user environment's AGENT_TEMP, TEMP or TMP); unset or skipped: the first of these clear of every session temp: `kit-temp`, then `kit-work`, at the root of the checkout's drive (not the system temp folder's drive), the system temp folder (%LOCALAPPDATA%\\Temp; on Linux /tmp), `kit-work` beside it (docs/test-tools.md \"Temp folders\"); older kit copies reject the key"),
    'machine_path_folders': Field(ListOf(Str(pattern=r'[^\\/:*?"<>|\s]+', what='a folder name')), None, 'the drive-rooted folder names the machine-paths check refuses besides a user profile (docs/commit.md "machine_paths.py"); unset: none, so only drive-rooted user-profile paths are refused and tools/machine_paths.py prints a NOTE saying so; older kit copies reject the key; set it only after every folder runs a kit that knows it'),
    'agent_temp_sweep': Field(Owned('optional/agenttemp/agent_temp_sweep.py'), None, "the agents' temp folder to sweep (`dir`), the age past which an unused entry goes (`max_age_days`, default 3), the entries it never removes (`keep`, relative paths such as `claude/<project>/<session>`) and whether it also sweeps the session folders under the user's own temp, HKCU\\Environment TEMP, else TMP (`user_temp`, true or false, default false; older sweeps refuse the key); `tools/machine_setup.py` adds it when absent (docs/machine-setup.md \"Extras\")"),
    'dev_drive': Field(AbsPath(), None, 'the Dev Drive\'s root folder: the gate and landing result records go under it, in `<dev_drive>\\pm-kit\\kit-gate-result` (docs/gate.md "Result record"), and `delegated.py run --approval` runs an operation, and `delegated.py request` asks for one, only on paths under it and outside every checkout (docs/delegations.md "Hub approvals"); unset: a gate writes no result record, every landing refuses, and every approval run and request refuses; older kit copies reject the key; set it only after every folder runs a kit that knows it'),
    'plugin': Field(Owned('kitlib.pluginsource'), None, 'where the plugin export of pushed kit main lives, and whether pm_watch refreshes it on its own (docs/plugin-source.md); older kit copies reject the key; set it only after every folder is migrated'),
}

# Which of the PM repository's two files holds each key (kitlib.config.move writes them so;
# tests/config/test_read_pm_repo.py keeps these equal to move's POLICY, LOCAL, DROPPED and
# RELATIVE_KEYS: this module does not import move, which imports kitlib.coordinatorstart).
POLICY_KEYS = ('ledger_path', 'ledger_dir', 'ledger_repos', 'pending_path',
               'guard_rules', 'guard_mark_roots', 'guard_agent_marks',
               'guard_mcp_tools', 'guard_mcp_prefix', 'pm_start', 'coordinator_start', 'pm_watch',
               'machine_lock', 'temp_dir', 'agent_temp_sweep',
               'machine_path_folders', 'plugin')
LOCAL_KEYS = ('owner_email', 'scrub_patterns', 'launcher', 'hub', 'dev_drive')
# Never in the PM repository's files: a ConfigError there, except a RETIRED key, which is ignored
# (`pm_data` is derived: locate).
DROPPED_KEYS = ('ledger_refs', 'pm_data', 'standing_delegation', 'daily_red_ruling', 'backlog_lint')
# POLICY_KEYS paths that may be written relative to the PM repository (resolved).
RELATIVE_KEYS = ('ledger_dir', 'ledger_path', 'pending_path')


class HomeFenced(BaseException):
    """home_dir() resolved a real home folder in a fenced process (the kit's tests and their
    children): one fence_home() fenced, or one whose environment carries FENCE_ENV. The machine
    lock raises it too, for a use of the machine's lock store there (kitlib.lock.store.fence). A
    BaseException, so the `except Exception` that isolates a check (pmstart's "could not check"
    lines, pmwatch's third failure) cannot turn the refusal into a line a test may miss."""


FENCE_MESSAGE = ('home lookup refused under the kit\'s tests: it resolved the real home folder; '
                 'give it an explicit home or folders (a temp folder), or patch USERPROFILE/HOME '
                 'to one (docs/test-tools.md "Home fence")')
UNPROVEN_MESSAGE = ('home lookup refused under the kit\'s tests: the system did not name the profile '
                    'folder (%s), so no home can be shown to be a temp folder (docs/test-tools.md '
                    '"Home fence")')
# The fence's marker: the home folder the kit's test run started with, in the environment of the
# test units (tools/kit_tests.py run_pass) and of a process that imports the tests package
# (tests/__init__.py), so the tools a test starts inherit it. Set by fence_env only; read by
# home_dir() and fenced() only, at each call. Unset or empty: no marker. A real run never has it.
FENCE_ENV = 'KIT_HOME_FENCE'
# The real home folder (_home_key) recorded by fence_home(); None: fence_home() was not called.
_FENCED_HOME = None
# (real_profile()'s folder, its _real_key) once the system was asked; None before.
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


def _system_profile():
    """The user's profile folder as the system reports it: Windows's (_windows_profile), or on
    Linux the password database's (kitlib.platform.linux.paths.profile). OSError when it does
    not answer."""
    if kitlib.platform.NAME == 'linux':
        return linux_paths.profile()
    return _windows_profile()


def _profile():
    global _PROFILE
    if _PROFILE is None:
        path = _system_profile()
        _PROFILE = (path, _real_key(path))
    return _PROFILE


def real_profile():
    """The user's profile folder from the system (Windows, or Linux's password database), never
    from the environment: no USERPROFILE or HOME moves it. Asked once per process. It names the
    folder and reads nothing in it; the fence compares against it, so it is not itself fenced.
    OSError when the system does not answer."""
    return _profile()[0]


def _plat(platform):
    """`platform` as sys.platform spells it: the one given, else the running one's (from
    kitlib.platform.NAME, read now)."""
    if platform is not None:
        return platform
    return 'win32' if kitlib.platform.NAME == 'windows' else kitlib.platform.NAME


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


def fenced():
    """Whether this process is under the kit's tests: fence_home() was called here, or the
    environment carries FENCE_ENV. One global read and one environment read; Windows is not asked
    and nothing is resolved, so it says nothing about which home a lookup would give. For code that
    must refuse a real machine resource under the tests without a home lookup
    (kitlib.lock.store.fence)."""
    return _FENCED_HOME is not None or bool(os.environ.get(FENCE_ENV))


def _refused(h, marker):
    """Whether the home `h` is a real one: the folder fence_home() recorded, the one `marker`
    names, or the profile folder the system reports, each compared as spelled and as the folder it
    really is (_real_key). HomeFenced when the system does not answer: without its profile folder no
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


def system_temp(environ=None, platform=None):
    """The system's own per-user temp folder, whatever TEMP/TMP this process inherited.
    Windows: %LOCALAPPDATA%/Temp, and a RuntimeError when LOCALAPPDATA is
    unset or empty: falling back to TEMP would split logs between processes. Linux: /tmp
    (kitlib.platform.linux.paths.temp), whatever TMPDIR says, for the same reason. Any other
    platform keeps tempfile.gettempdir(). No machine config is read."""
    env = os.environ if environ is None else environ
    plat = _plat(platform)
    if plat.startswith('linux'):
        return linux_paths.temp()
    if plat != 'win32':
        return tempfile.gettempdir()
    lad = env.get('LOCALAPPDATA')
    if not lad:
        raise RuntimeError('LOCALAPPDATA is unset or empty: the system temp folder '
                           '(%LOCALAPPDATA%/Temp) cannot be resolved, and following TEMP instead '
                           'would split logs between processes')
    return os.path.join(lad, 'Temp')


def temp_parent(home=None):
    """The machine config's `temp_dir`, or None: unset, a machine config that cannot be read, a
    folder whose drive (or share) root is not there, or a fenced process (fenced()) given no
    `home`, so the tests never see this machine's config. Never raises for the config."""
    if home is None and fenced():
        return None
    try:
        configured = user_config(home)['temp_dir']
    except ConfigError:
        return None
    if not configured:
        return None
    root = os.path.splitdrive(os.path.abspath(configured))[0]
    if root and not os.path.isdir(root + os.sep):
        return None
    return configured


def shared_temp(environ=None, platform=None, home=None):
    """The temp folder every process on the machine shares, whatever TEMP/TMP it inherited, for
    the kit's few small files there (gate fallback logs, the worktree sweep log, comfy_gen's
    state; private index folders go in the same `temp_dir` through mkdtemp): the
    machine config's `temp_dir` (temp_parent), even
    when it is a session's TEMP, made here if missing since its callers open files in it at once;
    else, or when it cannot be made, system_temp(). Only work_temp keeps the heavy churn out of
    session temps. RuntimeError as system_temp, when `temp_dir` is not used."""
    parent = temp_parent(home)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
            return parent
        except OSError:
            pass
    return system_temp(environ, platform)


def mkdtemp(prefix, home=None):
    """A new folder (tempfile.mkdtemp, `prefix`) in temp_parent(home), that folder made if
    missing; with no temp_parent, or one that cannot be made or written, in this process's temp
    folder. For short-lived private folders (a private git index). Never a config error."""
    parent = temp_parent(home)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
            return tempfile.mkdtemp(prefix=prefix, dir=parent)
        except OSError:
            pass
    return tempfile.mkdtemp(prefix=prefix)


def _drive(path):
    return os.path.normcase(os.path.splitdrive(os.path.abspath(path))[0])


def _user_env(name):
    """The user environment value `name` (HKCU\\Environment) as Windows stores it for new
    processes, REG_EXPAND_SZ expanded; None when unset, empty, not a string or unreadable."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            value, kind = winreg.QueryValueEx(key, name)
        if not isinstance(value, str) or not value:
            return None
        if kind == winreg.REG_EXPAND_SZ:
            value = winreg.ExpandEnvironmentStrings(value)
        return value or None
    except (ImportError, OSError):
        return None


def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _overlaps(a, b):
    """Whether the folders `a` and `b` are the same folder or one holds the other, by their
    spelled paths (case and separators normalised, links not followed)."""
    a, b = _norm(a), _norm(b)
    if a == b:
        return True
    ra, rb = a.rstrip(os.sep) + os.sep, b.rstrip(os.sep) + os.sep
    return a.startswith(rb) or b.startswith(ra)


def _sessions(sweep, reg):
    """session_temps' list from the config's agent_temp_sweep value `sweep` and the reader `reg`
    (None: the user environment, or no reader at all in a fenced process)."""
    found = []
    d = sweep.get('dir') if isinstance(sweep, dict) else None
    if isinstance(d, str) and os.path.isabs(d):
        found.append(d)
    if reg is None and not fenced():
        reg = _user_env
    for name in SESSION_VARS if reg is not None else ():
        v = reg(name)
        if isinstance(v, str) and os.path.isabs(v):
            found.append(v)
    out = []
    for p in found:
        p = _norm(p)
        if p not in out:
            out.append(p)
    return out


def session_temps(home=None, reg=None):
    """The folders sessions on this machine use as TEMP, which the kit's churn keeps out of
    (work_temp): the machine config's agent_temp_sweep `dir` (the agents' temp folder machine
    setup made) and the user environment's SESSION_VARS (HKCU\\Environment, expanded), each
    absolute, normalised, once. `reg(name)` (value or None) replaces the user environment reader;
    a fenced process (fenced()) reads no registry unless given one, and with no `home` no config,
    so the tests never see this machine's. The process environment is never read, so every process
    agrees. A machine config that cannot be read counts as none."""
    sweep = None
    if home is not None or not fenced():
        try:
            sweep = user_config(home)['agent_temp_sweep']
        except ConfigError:
            sweep = None
    return _sessions(sweep, reg)


def _default_candidates(near, environ, plat):
    """work_temp's candidates after the configured `temp_dir`, on Windows: TEMP_DIR_NAME and
    WORK_DIR_NAME at the root of `near`'s drive when that root exists and is not the system temp
    folder's drive, then the system temp folder and WORK_DIR_NAME beside it (when LOCALAPPDATA is
    set)."""
    try:
        shared = system_temp(environ, plat)
    except RuntimeError:
        shared = None
    out = []
    drive = os.path.splitdrive(os.path.abspath(near))[0]
    if drive and os.path.isdir(drive + os.sep) and (shared is None
                                                    or _drive(near) != _drive(shared)):
        out += [os.path.join(drive + os.sep, TEMP_DIR_NAME),
                os.path.join(drive + os.sep, WORK_DIR_NAME)]
    if shared is not None:
        out += [shared, os.path.join(os.path.dirname(shared), WORK_DIR_NAME)]
    return out


def _pick(candidates, sessions):
    """The first of `candidates` that overlaps none of `sessions`, else the first: a slow gate,
    never a broken one."""
    for c in candidates:
        if not any(_overlaps(c, s) for s in sessions):
            return c
    return candidates[0]


def work_temp(near, home=None, environ=None, platform=None, reg=None):
    """The folder the kit's own temp files for the checkout `near` go in (test run folders and
    their fixture repositories, gate logs), so they stay on the checkout's drive (a Dev Drive,
    say) instead of the system drive, and out of every folder sessions use as TEMP
    (session_temps): a gate's thousands of files a second there slow every session that watches
    its TEMP. On Windows, the first of these that is not, does not hold and is not inside a
    session temp:
    - the machine config's `temp_dir` when set;
    - TEMP_DIR_NAME, then WORK_DIR_NAME, at the root of the drive (or UNC share) `near` is on,
      when that root exists and is not the drive the system temp folder is on;
    - system_temp(), then WORK_DIR_NAME beside it;
    and the first of them when every one overlaps. Elsewhere: `temp_dir`, else system_temp().
    It reads `near`, the machine config, LOCALAPPDATA and the user environment (`reg`, as
    session_temps), never this process's TEMP, TMP or AGENT_TEMP, so a gate writing logs and a
    reader in another shell agree. It makes nothing: a caller that cannot make its folder here
    falls back to shared_temp() (gate logs) or default_work_temp (make_temp).
    RuntimeError: no LOCALAPPDATA and no usable drive root; ConfigError: a bad machine config."""
    cfg = user_config(home)
    configured = cfg['temp_dir']
    plat = _plat(platform)
    if plat != 'win32':
        return configured or system_temp(environ, plat)
    candidates = ([configured] if configured else []) + _default_candidates(near, environ, plat)
    if not candidates:
        return system_temp(environ, plat)       # raises, saying why
    return _pick(candidates, _sessions(cfg['agent_temp_sweep'], reg))


def default_work_temp(near, environ=None, platform=None, reg=None, sessions=()):
    """work_temp(near) without the machine config's `temp_dir`: the kit's default folder, for a
    caller whose work_temp cannot be used or read (make_temp, tests/gitfixture.py). It keeps out
    of session_temps(reg=reg) (no config: under the fence that is nothing) and of the extra
    folders `sessions`. Elsewhere than Windows: system_temp(). RuntimeError as work_temp."""
    plat = _plat(platform)
    if plat != 'win32':
        return system_temp(environ, plat)
    candidates = _default_candidates(near, environ, plat)
    if not candidates:
        return system_temp(environ, plat)       # raises, saying why
    avoid = [_norm(s) for s in sessions if s] + session_temps(None, reg)
    return _pick(candidates, avoid)


def make_temp(near, prefix, home=None):
    """(folder, note): a new folder (tempfile.mkdtemp, `prefix`) in work_temp(near), that parent
    made if missing; else, with a note saying why, one in default_work_temp(near) when that is
    another folder and can be used, else one from mkdtemp (the machine config's `temp_dir`, else
    this process's temp folder)."""
    parent = None
    try:
        parent = work_temp(near, home)
        os.makedirs(parent, exist_ok=True)
        return tempfile.mkdtemp(prefix=prefix, dir=parent), None
    except (OSError, RuntimeError, ConfigError) as e:
        why = '%s could not be used (%s)' % (parent or 'the kit temp folder', e)
    try:
        fallback = default_work_temp(near)
        if parent is None or _norm(fallback) != _norm(parent):
            os.makedirs(fallback, exist_ok=True)
            folder = tempfile.mkdtemp(prefix=prefix, dir=fallback)
            return folder, '%s is in %s: %s' % (folder, fallback, why)
    except (OSError, RuntimeError):
        pass
    folder = mkdtemp(prefix, home=home)
    where = os.path.dirname(folder)
    if _norm(where) == _norm(tempfile.gettempdir()):
        return folder, '%s is in this process\'s temp folder: %s' % (folder, why)
    return folder, '%s is in %s: %s' % (folder, where, why)


class Location(collections.namedtuple('Location', 'state path local folder pm pm_data leftover error home new')):
    """Which machine config is read (locate).

    `state` OK, NONE, UNMOVED, LEFTOVER or BAD. `path` the policy file:
    `<PM repo>/.pm-kit/config.json` for OK and LEFTOVER, the known-absent home file
    `~/.pm-kit/config.json` for NONE, the home folder's leftover file for UNMOVED, and for BAD the PM repository's file when the pointer named
    one, else the home file. `local` LOCAL_FILE_NAME beside `path` for OK and LEFTOVER, else None.
    `folder` the PM repository's `.pm-kit` for OK and LEFTOVER, else None. `pm` the PM repository
    (the pointer's value, normalised) for OK and LEFTOVER, else None. `pm_data` the derived
    {path, branch, remote} for OK and LEFTOVER when the repository's kit-pm-data.json record is
    there, else None. `leftover` a machine config file still in the home folder (either folder
    name; UNMOVED, LEFTOVER), else None. `error` why the config cannot be read (UNMOVED,
    LEFTOVER, BAD), else None. `home` the home folder.

    `new` exists only for kitlib.hubsite.auth.folder (the hub's credential folder
    beside it) and the guard's matching hold on that folder (kitlib.guard.rules.hub_secret_folders);
    the hub session removes it: `path` for OK, else the home file."""

    def display(self, path=None):
        """`path` (default the policy file) as messages name it: `~/<path>` under the home
        folder, else the full path."""
        return _shown(path or self.path, self.home)


class HomeLocation(collections.namedtuple('HomeLocation', 'state path new old error home')):
    """The machine config file in the home folder (home_location), the move's source
    (kitlib.config.move): `state` one of NEW, OLD, BOTH, NONE, BAD; `path` the file read
    (DIR_NAME's for NONE and BAD); `new` and `old` the two candidate files; `error` why a BAD
    location cannot be read, else None; `home` the home folder."""

    def display(self, path=None):
        """`path` (default the file read) as ~/<folder>/<file>."""
        p = path or self.path
        return '~/%s/%s' % (os.path.basename(os.path.dirname(p)), os.path.basename(p))


def _shown(path, home):
    try:
        rel = os.path.relpath(path, home)
    except ValueError:                       # another drive
        return path
    if rel == os.curdir or rel.startswith(os.pardir) or os.path.isabs(rel):
        return path
    return '~/' + rel.replace('\\', '/')


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


def home_location(home=None):
    """The HomeLocation of the machine config file in the home folder `home` (default the
    user's): the move's source (kitlib.config.move), never what the kit reads (locate). It
    decides on DIR_NAME's config file, never on its folder alone, so another writer making the
    folder first does not hide the old file. It reads nothing but the file system's answers and
    never raises."""
    h = home or home_dir()
    new_dir, old_dir = os.path.join(h, DIR_NAME), os.path.join(h, OLD_DIR_NAME)
    new, old = os.path.join(new_dir, FILE_NAME), os.path.join(old_dir, FILE_NAME)

    def bad(why):
        return HomeLocation(BAD, new, new, old, why, h)
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
            return HomeLocation(NEW, new, new, old, None, h)
        return bad('cannot tell whether the machine config %s exists: %s' % (old, e))
    if n == 'file':
        return HomeLocation(NEW if o == 'absent' else BOTH, new, new, old, None, h)
    if o == 'absent':
        return HomeLocation(NONE, new, new, old, None, h)
    return HomeLocation(OLD, old, new, old, None, h)


# The pointer's answer, once per process for each raw registry answer
# (kitlib.config.pointer.read_raw): checking it starts git, and one tool reads the machine config
# many times.
_POINTERS = {}


def _pointer():
    from . import pointer                    # pointer imports this module
    raw = pointer.read_raw()
    if raw not in _POINTERS:
        _POINTERS[raw] = pointer.read(raw=raw)
    return _POINTERS[raw]


def forget():
    """Drop the remembered pointer answers (_pointer): for a test that changes the PM repository
    a pointer value names, or the pointer, inside one process."""
    _POINTERS.clear()


def _leftover(h):
    """(the first machine config file still in the home folder `h`, DIR_NAME's then the retired
    folder's, or None; why the home folder cannot be examined, or None)."""
    for name in (DIR_NAME, OLD_DIR_NAME):
        path = os.path.join(h, name, FILE_NAME)
        try:
            if _kind(path) != 'absent':
                return path, None
        except OSError as e:
            return None, 'cannot tell whether the machine config %s exists: %s' % (path, e)
    return None, None


MOVE_DOC = 'docs/kit-json.md "Moving the machine config into the PM repo"'
POINTER_DOC = 'docs/machine-setup.md "PM repo pointer"'
# LEFTOVER's error: (the PM repository's policy file, the home file, the home file with each '
# doubled for PowerShell's single-quoted string, MOVE_DOC).
LEFTOVER_WHY = ("the machine config moved to the PM repository (%s); a copy is still in the home "
                "folder (%s) and the kit stops until it is gone: remove it (Remove-Item -LiteralPath "
                "'%s'), %s")


def locate(home=None, ptr=None):
    """The Location of the machine config: the PM repository's when the PM-repo pointer is good
    (`ptr`, a kitlib.config.pointer.Pointer already taken, else this process's, read once per
    registry answer), under the home folder `home` (default the user's) for the leftover check.
    It reads the pointer and the file system's answers only and never raises."""
    h = home or home_dir()
    home_file = os.path.join(h, DIR_NAME, FILE_NAME)
    p = ptr if ptr is not None else _pointer()

    def make(state, path, error=None, local=None, folder=None, pm=None, pm_data=None, leftover=None):
        return Location(state, path, local, folder, pm, pm_data, leftover, error, h,
                        path if state == OK else home_file)

    def unmoved(leftover, pm_file):
        if pm_file:
            why = ('the machine config is still in the home folder (%s) and the PM repository has '
                   'none (%s): move it with tools/config_move.py (%s)' % (leftover, pm_file, MOVE_DOC))
        else:
            why = ('the machine config is still in the home folder (%s), and the kit reads it from '
                   'the PM repository now: set the PM repo pointer (tools/machine_setup.py '
                   '--pm-repo, %s), then move it with tools/config_move.py (%s)'
                   % (leftover, POINTER_DOC, MOVE_DOC))
        return make(UNMOVED, leftover, why, leftover=leftover)

    if p.state == BAD_POINTER:
        return make(BAD, home_file, 'the PM repo pointer cannot be used, so the machine config '
                    'cannot be read: %s (%s)' % (p.error, POINTER_DOC))
    if p.state != OK_POINTER:
        leftover, why = _leftover(h)
        if why:
            return make(BAD, home_file, why)
        if leftover:
            return unmoved(leftover, None)
        return make(NONE, home_file)
    pm = os.path.normpath(p.path)
    folder = os.path.join(pm, DIR_NAME)
    path, local = os.path.join(folder, FILE_NAME), os.path.join(folder, LOCAL_FILE_NAME)
    try:
        d = _kind(folder)
        if d not in ('absent', 'dir'):
            return make(BAD, path, '%s is not a folder: the machine config cannot be read' % folder)
        kinds = [(f, _kind(f) if d == 'dir' else 'absent') for f in (path, local)]
    except OSError as e:
        return make(BAD, path, 'cannot tell whether the machine config in %s exists: %s' % (folder, e))
    for f, k in kinds:
        if k not in ('absent', 'file'):
            return make(BAD, path, '%s is not a file: the machine config cannot be read' % f)
    leftover, why = _leftover(h)
    if all(k == 'absent' for _f, k in kinds):
        if why:
            return make(BAD, path, why)
        if leftover:
            return unmoved(leftover, path)
    record = getattr(p, 'record', None)
    pm_data = None
    if record is not None:
        pm_data = {'path': pm, 'branch': record.branch, 'remote': record.remote}
    if leftover:
        why = LEFTOVER_WHY % (path, leftover, leftover.replace("'", "''"), MOVE_DOC)
        return make(LEFTOVER, path, why, local=local, folder=folder, pm=pm, pm_data=pm_data,
                    leftover=leftover)
    return make(OK, path, local=local, folder=folder, pm=pm, pm_data=pm_data)


def config_dir(home=None):
    """The PM repository's machine config folder (`<PM repo>/.pm-kit`) when locate() is OK, else
    None (pm_config_dir). The home folder's `.pm-kit` is never named in its place; a caller that
    only keeps clear of the folders holding config uses keep_clear_dirs. Never raises."""
    return pm_config_dir(home)


def pm_config_dir(home=None):
    """The PM repository's machine config folder (`<PM repo>/.pm-kit`) when locate() is OK, else
    None: with no good pointer, or a config that cannot be read, there is no such folder, and the
    home folder's `.pm-kit` is never named in its place (keep_clear_dirs does, for the callers that
    only keep clear of it). Never raises."""
    loc = locate(home)
    return loc.folder if loc.state == OK else None


def keep_clear_dirs(home=None):
    """Every folder that still holds the machine config, for a caller that only keeps clear of
    them (the agent-temp sweep and machine setup): the PM repository's `.pm-kit` for OK and
    LEFTOVER, the folder of the config file still in the home folder for UNMOVED and LEFTOVER;
    for BAD the PM repository's `.pm-kit` when the pointer is good (the config cannot be read, but
    the folder may exist), else nothing; none for NONE (no config is known to be anywhere).
    Never raises."""
    loc = locate(home)
    dirs = []
    if loc.state in (OK, LEFTOVER) and loc.folder:
        dirs.append(loc.folder)
    elif loc.state == BAD:
        p = _pointer()
        if p.state == OK_POINTER:
            dirs.append(os.path.join(os.path.normpath(p.path), DIR_NAME))
    if loc.state in (UNMOVED, LEFTOVER) and loc.leftover:
        dirs.append(os.path.dirname(loc.leftover))
    return dirs


def config_file(home=None):
    """The machine config's policy file locate() names, even for UNMOVED, LEFTOVER and BAD. Never raises."""
    return locate(home).path


def readable_file(home=None):
    """The machine config's policy file a reader opens (locate()): the PM repository's
    config.json for OK, the known-absent home file for NONE. ConfigError when the config cannot
    be read (UNMOVED, LEFTOVER, BAD): a reader that opens the file itself (pmstart, pmwatch,
    ...) must not take a file it cannot use for an absent one."""
    loc = locate(home)
    if loc.error:
        raise ConfigError(loc.error)
    return loc.path


def display(home=None):
    """The machine config's policy file, as messages name it (Location.display)."""
    return locate(home).display()


def key_file(home=None, key=None):
    """The machine config file that holds `key`, as messages name it: for OK the PM repository's
    LOCAL_FILE_NAME for a LOCAL_KEYS key, else locate()'s policy file. Never raises."""
    loc = locate(home)
    return loc.local if loc.state == OK and key in LOCAL_KEYS else loc.path


def writable_file(home=None, key=None):
    """The file a writer of the machine config key `key` writes: for OK the PM repository's
    LOCAL_FILE_NAME when `key` is a LOCAL_KEYS key, else its FILE_NAME. ConfigError when the
    config cannot be read (UNMOVED, LEFTOVER, BAD), for OK when `key` is one the PM repository's config
    never holds (DROPPED_KEYS: `pm_data` is derived), and for NONE: with no pointer there is no
    file to write, and the home file is never made again (it would be a leftover, UNMOVED)."""
    loc = locate(home)
    if loc.error:
        raise ConfigError(loc.error)
    if loc.state == OK:
        if key in DROPPED_KEYS:
            raise ConfigError(_dropped(key, loc.folder))
        return loc.local if key in LOCAL_KEYS else loc.path
    raise ConfigError('no PM repo pointer is set, so the machine config has no file to write%s '
                      'to: set the pointer first (tools/machine_setup.py --pm-repo <PM repository>, '
                      'an elevated owner step, %s)' % (' ' + key if key else '', POINTER_DOC))


def _dropped(key, where):
    if key == 'pm_data':
        return ("%s: pm_data is derived from the PM repo pointer and its repository's "
                "kit-pm-data.json record; the machine config never holds it" % where)
    return '%s: %s is a key the move drops (%s); nothing reads it here' % (where, key, MOVE_DOC)


def _read_object(path):
    """The JSON value of the file `path`, or None when it is absent. ConfigError when it cannot
    be read or parsed."""
    try:
        with open(path, encoding='utf-8-sig') as f:
            text = f.read()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ConfigError('cannot read %s: %s' % (path, e))
    return parse_json(text, path)


def _read_home(path):
    data = _read_object(path)
    if data is None:
        return {}
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k not in RETIRED}
    errs = []
    Obj(SCHEMA, what='machine config').check(data, path, errs)
    if not errs:
        errs = ['%s: %s' % (path, why) for why in ledger_errors(data)]
    if errs:
        raise ConfigError('; '.join(errs))
    return data


def _read_pm(loc):
    """The PM repository's two files merged, each key from its own file, RELATIVE_KEYS read
    against the PM repository. ConfigError naming every problem."""
    merged, errs = {}, []
    files = ((loc.path, POLICY_KEYS, loc.local, LOCAL_KEYS), (loc.local, LOCAL_KEYS, loc.path, POLICY_KEYS))
    for path, own, other, other_keys in files:
        data = _read_object(path)
        if data is None:
            continue
        if not isinstance(data, dict):
            errs.append('%s must be an object' % path)
            continue
        kept = {}
        for k, v in data.items():
            if is_note(k) or k in RETIRED:
                continue
            if k in DROPPED_KEYS:
                errs.append(_dropped(k, path))
            elif k in other_keys:
                errs.append('%s: %s belongs in %s, not here' % (path, k, other))
            else:
                kept[k] = v
        if own is POLICY_KEYS:
            kept = resolved(kept, loc.pm)
        Obj({k: SCHEMA[k] for k in own}, what='machine config').check(kept, path, errs)
        merged.update(kept)
    if not errs:
        errs = ['%s: %s' % (loc.path, why) for why in ledger_errors(merged)]
    if errs:
        raise ConfigError('; '.join(errs))
    return merged


def resolved(policy, pm):
    """`policy` with its relative RELATIVE_KEYS paths read against the PM repository `pm`."""
    out = dict(policy)
    for k in RELATIVE_KEYS:
        v = out.get(k)
        if isinstance(v, str) and v.strip() and not os.path.isabs(v):
            out[k] = os.path.normpath(os.path.join(pm, v))
    return out


def user_config(home=None, location=None):
    """The checked machine config, every schema key present (None where unset). `location` is a
    Location already taken (the guard takes one per call), else locate(home); a HomeLocation
    (home_location) reads the home folder's one file, as the move does. `pm_data` is the
    Location's derived one."""
    loc = location or locate(home)
    if loc.error:
        raise ConfigError(loc.error)
    if isinstance(loc, HomeLocation):
        data = _read_home(loc.path)
    elif loc.state == OK:
        data = _read_pm(loc)
    else:
        data = {}
    out = {k: f.default for k, f in SCHEMA.items()}
    out.update({k: v for k, v in data.items() if k in SCHEMA})
    if isinstance(loc, Location):
        out['pm_data'] = loc.pm_data
    return out


LEDGER_NOT_A_REPO = ('all', 'standing')    # `all` is gone with the folder; standing.md is the digest


def ledger_errors(data):
    """What the ledger keys refuse together, for a machine config whose keys each passed their
    type: `ledger_dir` needs `ledger_repos`, and each name's file `<ledger_dir>/<name>.md` must
    be its own (a name once, not `all`, not the digest's, not the frozen file's)."""
    folder = data.get('ledger_dir')
    if folder is None:
        return []
    repos = data.get('ledger_repos')
    if not repos:
        return ['ledger_dir needs ledger_repos: the repos with a file in that folder']
    out, seen = [], set()
    frozen = data.get('ledger_path')
    frozen = os.path.normcase(os.path.normpath(frozen)) if isinstance(frozen, str) else None
    for name in repos:
        if name in seen:
            out.append('ledger_repos names %r twice' % name)
        elif name in LEDGER_NOT_A_REPO:
            out.append('ledger_repos may not name %r with ledger_dir set' % name)
        elif os.path.normcase(os.path.normpath(os.path.join(folder, name + '.md'))) == frozen:
            out.append('ledger_repos may not name %r: its file is ledger_path, the frozen ledger' % name)
        seen.add(name)
    return out


def failing_keys(data):
    """The sorted top-level keys of the machine config object `data` that user_config refuses:
    unknown, or holding a value its type refuses, or ledger_repos when ledger_errors refuses the
    pair. Names only: no value is returned, so a caller may print them where the value must not
    appear (kitlib.preflight.configline)."""
    out = []
    for k, v in data.items():
        if is_note(k) or k in RETIRED:
            continue
        f = SCHEMA.get(k)
        errs = []
        if f is None:
            errs.append(k)
        elif not (v is None and f.default is None):
            f.type.check(v, k, errs)
        if errs:
            out.append(k)
    if not {'ledger_dir', 'ledger_repos', 'ledger_path'} & set(out) and ledger_errors(data):
        out.append('ledger_repos')
    return sorted(out)


def owner_email(home=None, location=None):
    """The machine's owner email (lower case), or None when the machine config sets none. A
    machine config that cannot be read raises ConfigError; the caller fails closed on it."""
    value = user_config(home, location)['owner_email']
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def ledger_path(home=None):
    """The ledger path, or None when the machine config does not set one."""
    return user_config(home)['ledger_path']


def ledger_repos(home=None):
    """The repos the ledger writer accepts, or None (one-file ledger only: any lower-case name)."""
    return user_config(home)['ledger_repos']


def ledger_dir(home=None):
    """The per-repo ledger folder, or None when the machine config does not set one."""
    return user_config(home)['ledger_dir']


def files_of(cfg):
    """{repo: <ledger_dir>/<repo>.md} in ledger_repos order, from a config user_config returned;
    {} when it sets no ledger_dir."""
    folder = cfg.get('ledger_dir')
    if not folder:
        return {}
    return {name: os.path.join(folder, name + '.md') for name in cfg.get('ledger_repos') or ()}


def ledger_files(home=None):
    """files_of the machine config."""
    return files_of(user_config(home))


def machine_path_folders(home=None):
    """The drive-rooted folder names the machine-path check refuses besides a user profile, as a
    tuple: the machine config's `machine_path_folders`, else () (the kit codes no folder names).
    A machine config that cannot be read raises ConfigError."""
    return tuple(user_config(home)['machine_path_folders'] or ())


def dev_drive(cfg):
    """The Dev Drive's root folder from a config user_config returned, or None when it sets none
    (kitlib.delegations.place refuses every approval run then)."""
    value = cfg.get('dev_drive')
    return value if isinstance(value, str) and value.strip() else None


def doc_lines():
    return ['- `%s` (%s): %s' % (k, f.type.describe(), f.doc) for k, f in SCHEMA.items()]
