"""The machine's own kit settings: `~/.claude-pm-kit/config.json`.

The ledger path is read from this file only: no `.kit.json` key, no ledger environment variable
and no command-line flag sets it, so a repository cannot point the ledger somewhere else. `~` is
os.path.expanduser's: USERPROFILE on Windows, HOME elsewhere, so an environment that changes that
variable changes which machine config is read. An absent file is an empty config; a present file that cannot be read, or that
holds an unknown key or a bad value, is a ConfigError (never silently "no ledger").
"""
import os
import sys
import tempfile

from .types import ConfigError, Enum, Field, ListOf, MapOf, Obj, Regex, Str, T, parse_json

DIR_NAME = '.claude-pm-kit'
FILE_NAME = 'config.json'
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
    'guard_agent_marks': Field(ListOf(Str()), None, 'the session marks the guard treats as agent sessions for the kill-by-name, lock-state, kit-json and write-jail rules; it replaces the default list, so it names every mark it holds; unset: coordinator, conductor, deep-worker, consultant and pm (docs/guard.md "Session mark")'),
    'guard_mark_roots': Field(MarkMap(ListOf(MARK_ROOT), what='mark'), None, 'more write-jail roots for the main sessions of one launcher mark, as {mark: [{path, mode, why}]}; a root that is, holds or lies inside this config\'s own folder is ignored (docs/guard.md "Write jail")'),
    'pm_data': Field(Owned('kitlib.pmdata'), None, 'the worktree of the PM data branch: {path, branch, remote} (docs/pm-data-branch.md "Layout")'),
    'pm_start': Field(Owned('kitlib.pmstart'), None, 'the PM session-start checklist settings'),
    'coordinator_start': Field(Owned('kitlib.coordinatorstart'), None, 'the coordinator session-start settings'),
    'conductor_start': Field(Owned('kitlib.coordinatorstart'), None, 'the old name of coordinator_start, read when coordinator_start is absent'),
    'pm_watch': Field(Owned('kitlib.pmwatch'), None, 'the PM watcher settings'),
    'launcher': Field(Owned('kitlib.launcher'), None, 'the session launcher settings: state folder, repos, account folders (docs/launcher.md "Settings")'),
    'backlog_lint': Field(Owned('kitlib.backloglint'), None, 'the backlog lint repositories'),
    'machine_lock': Field(Owned('kitlib.lock.site'), None, 'the machine lock settings: build daemons at release, lock-window notices'),
    'temp_dir': Field(AbsPath(), None, "the folder the kit's own temp files go in (test run folders, gate logs); unset: `AgentTemp` at the root of the checkout's drive, or the shared temp folder on its drive (docs/test-tools.md \"Temp folders\"); older kit copies reject the key"),
    'agent_temp_sweep': Field(Owned('optional/agenttemp/agent_temp_sweep.py'), None, 'the agents\' temp folder to sweep and the age past which an unused entry goes'),
}


def home_dir():
    """The user's home folder (os.path.expanduser: USERPROFILE on Windows, HOME elsewhere)."""
    return os.path.expanduser('~')


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


def config_file(home=None):
    return os.path.join(home or home_dir(), DIR_NAME, FILE_NAME)


def user_config(home=None):
    """The checked machine config, every schema key present (None where unset)."""
    path = config_file(home)
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
