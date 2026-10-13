"""Where a path is, read from the filesystem: kitlib.git.repo.classify's answer without starting
git (docs/guard.md "State"). The guard asks for a place on most calls, and each git child costs
a process launch and the owner wants fewer launches per tool call, so the common layouts are
read here and everything else is handed to repo.classify unchanged.

classify(path) walks up from the real path of path's nearest existing directory, as git's own
discovery does, and at the first folder holding `.git`:

  .git is a folder that is a valid git directory      MAIN: top is that folder
  .git is a file `gitdir: X`, X a valid git directory  MAIN when X has no commondir, else LINKED
                                                        (X/commondir names the common dir)

Valid means what git checks: HEAD names a ref under refs/ or is a hex object id, and the common
dir has objects/ and refs/. Nothing found up to the drive root is NONE, with git's own message.

It answers only when the answer cannot differ from git's, and otherwise calls `fallback`
(repo.classify) with the same path:

  - an environment variable that changes discovery is set (FALLBACK_ENV; git's GIT_DIR and
    friends are dropped from every git child (kitlib.git.cmd), so those do not count)
  - the real path is not a plain drive path (UNC, \\\\?\\ volume paths)
  - the real path differs from the path as written other than in case: a junction or symlink
    on the way, or a subst drive (git walks the path as written, this reader the real one)
  - the walk crosses a volume boundary, or a folder cannot be read
  - a folder on the way is itself a git directory (git would answer from inside it)
  - .git is anything but the two shapes above, or a gitdir file that does not lead to a valid
    git directory
  - the repository config sets core.worktree or leaves core.bare true. The config is the common
    config and, while it turns extensions.worktreeConfig on, then <gitdir>/config.worktree (the
    main checkout's .git/config.worktree, a linked worktree's .git/worktrees/<name>/
    config.worktree), read after it so the last core.bare wins, as git reads them
  - the common config has any extension other than worktreeConfig, or a
    core.repositoryformatversion other than 0 or 1; config.worktree has [extensions] or
    core.repositoryformatversion
  - either file has [include] or [includeIf], a value continued onto the next line, a core.bare
    or worktreeConfig value other than true, yes, on, false, no, off, 0 or empty, or a line
    git's parser refuses (a key on its section's header line, `[core] bare = true`, is read as
    git reads it)
  - the work tree, the gitdir file or a git directory is not owned by the current user (git's
    safe.directory check; Windows only)
  - anything else goes wrong while reading (any Exception): git answers instead
"""
import os
import re

from kitlib.git import repo
from kitlib.guard import paths

NOT_A_REPO = 'fatal: not a git repository (or any of the parent directories): .git'
FALLBACK_ENV = ('GIT_OBJECT_DIRECTORY', 'GIT_DISCOVERY_ACROSS_FILESYSTEM', 'GIT_CONFIG_PARAMETERS',
                'GIT_CONFIG_COUNT', 'GIT_TEST_ASSUME_DIFFERENT_OWNER', 'GIT_ALTERNATE_OBJECT_DIRECTORIES')
_HEX = frozenset('0123456789abcdef')


class _Fallback(Exception):
    """The filesystem cannot answer the way git would: ask git."""


def classify(path, fallback=None, environ=None, owned=None):
    """Place(kind, top, detail, git_dir, common) for `path`, as repo.classify answers it."""
    fallback = fallback or repo.classify
    env = os.environ if environ is None else environ
    try:
        if any(env.get(k) for k in FALLBACK_ENV):
            raise _Fallback()
        return _walk(path, owned or owned_by_me)
    except Exception:                       # _Fallback, or a read that failed: git decides
        return fallback(path)


def _walk(path, owned):
    written = os.path.abspath(repo.existing_dir(path))
    start = os.path.realpath(written)
    if paths.norm(start) != paths.norm(written):
        raise _Fallback()                   # a junction, symlink or subst drive on the way
    plain = (start[:1].isalpha() and start[1:3] == ':\\') if os.name == 'nt' else start.startswith('/')
    if not plain:
        raise _Fallback()
    d = start
    dev = os.stat(d).st_dev
    while True:
        found = _at(d, owned)
        if found:
            return found
        if _is_git_dir(d):
            raise _Fallback()
        parent = os.path.dirname(d)
        if parent == d:
            return repo.Place(repo.NONE, None, NOT_A_REPO, None, None)
        if os.stat(parent).st_dev != dev:
            raise _Fallback()
        d = parent


def _at(d, owned):
    """The Place when folder `d` holds a .git git finds, None when it holds none."""
    dot = os.path.join(d, '.git')
    if os.path.isdir(dot):
        if not _is_git_dir(dot):
            raise _Fallback()               # git walks on past it; not worth reproducing
        if os.path.exists(os.path.join(dot, 'commondir')):
            raise _Fallback()
        _config_ok(dot, dot)
        if not (owned(d) and owned(dot)):
            raise _Fallback()
        g = repo.real(dot)
        return repo.Place(repo.MAIN, paths.norm(d, fold=False), '', g, g)
    if os.path.isfile(dot):
        with open(dot, 'rb') as f:
            data = f.read(4096).decode('utf-8')
        if not data.startswith('gitdir: '):
            raise _Fallback()
        target = data[len('gitdir: '):].strip()
        if not target or '\n' in target:
            raise _Fallback()
        gitdir = paths.norm(os.path.join(d, target), fold=False)
        common = _common(gitdir)
        if not _is_git_dir(gitdir, common):
            raise _Fallback()
        _config_ok(common, gitdir)
        if not (owned(d) and owned(dot) and owned(gitdir)):
            raise _Fallback()
        g, c = repo.real(gitdir), repo.real(common)
        kind = repo.MAIN if g == c else repo.LINKED
        return repo.Place(kind, paths.norm(d, fold=False), '', g, c)
    if os.path.lexists(dot):
        raise _Fallback()                   # a .git that is neither file nor folder
    return None


def _common(gitdir):
    p = os.path.join(gitdir, 'commondir')
    if not os.path.exists(p):
        return gitdir
    with open(p, 'rb') as f:
        target = f.read(4096).decode('utf-8').strip()
    if not target or '\n' in target:
        raise _Fallback()
    return paths.norm(os.path.join(gitdir, target), fold=False)


def _is_git_dir(gitdir, common=None):
    """git's is_git_directory: objects/ and refs/ in the common dir, a valid HEAD in gitdir."""
    common = common or _common_if_dir(gitdir)
    if common is None:
        return False
    if not (os.path.isdir(os.path.join(common, 'objects')) and os.path.isdir(os.path.join(common, 'refs'))):
        return False
    head = os.path.join(gitdir, 'HEAD')
    if not os.path.isfile(head):
        return False
    with open(head, 'rb') as f:
        text = f.read(512).decode('utf-8', 'replace')
    if text.startswith('ref:'):
        return text[4:].lstrip().startswith('refs/')
    sha = text.strip()
    return len(sha) in (40, 64) and set(sha) <= _HEX


def _common_if_dir(d):
    if not os.path.isdir(d):
        return None
    return _common(d)


def _config_ok(common, gitdir):
    """Raise _Fallback when the repository config changes where git puts the work tree, or when
    git would refuse the repository. git reads the common config, then, while the common config
    turns extensions.worktreeConfig on, <gitdir>/config.worktree; the last core.bare read wins."""
    bare, worktree_config, version = False, False, '0'
    for section, key, value in _entries(os.path.join(common, 'config')):
        if section == 'extensions':
            if key != 'worktreeconfig':
                raise _Fallback()           # git refuses some, ignores others: not worth telling apart
            worktree_config = _bool(value)
        elif section == 'core' and key == 'repositoryformatversion':
            version = value
        elif section == 'core' and key == 'bare':
            bare = _bool(value)
    if not version or not set(version) <= _DIGITS or int(version) > 1:
        raise _Fallback()                   # git refuses a version above 1 or one it cannot read
    if worktree_config:
        for section, key, value in _entries(os.path.join(gitdir, 'config.worktree')):
            if section == 'extensions' or (section == 'core' and key == 'repositoryformatversion'):
                raise _Fallback()
            if section == 'core' and key == 'bare':
                bare = _bool(value)
    if bare:
        raise _Fallback()


_DIGITS = frozenset('0123456789')
_SPACE = ' \t\r'                            # git's isspace, less the \n lines are split on
_HEADER = re.compile(r'\[([A-Za-z0-9.-]+)(?:[ \t\r]+"((?:[^"\\]|\\.)*)")?\](.*)$')
_KEY = re.compile(r'([A-Za-z][A-Za-z0-9-]*)[ \t]*(?:=(.*))?$')
_ESCAPES = {'t': '\t', 'b': '\b', 'n': '\n', '\\': '\\', '"': '"'}


def _bool(value):
    """git's boolean for a key that must be one (None: a key with no `=`, which is true)."""
    if value is None:
        return True
    v = value.lower()
    if v in ('true', 'yes', 'on'):
        return True
    if v in ('false', 'no', 'off', '0', ''):
        return False
    raise _Fallback()                       # a number, or a value git refuses


def _entries(path):
    """(section, key, value) for every key in config file `path`, as git's parser reads them:
    section and key lower-cased, section '' for a key under a subsection, value None for a key
    with no `=`. Raises _Fallback on what this reader does not reproduce: a line git refuses, a
    value continued onto the next line, an [include] or [includeIf], an [extensions] subsection,
    core.worktree."""
    if not os.path.exists(path):
        return []
    with open(path, 'rb') as f:
        text = f.read().decode('utf-8-sig', 'replace')
    out, section = [], None
    for line in text.split('\n'):
        s = line.lstrip(_SPACE)
        if s.startswith('['):
            m = _HEADER.match(s)
            if not m:
                raise _Fallback()
            name, sub, s = m.group(1).lower(), m.group(2), m.group(3).lstrip(_SPACE)
            base = name.split('.', 1)[0]
            if base in ('include', 'includeif') or (base == 'extensions' and (sub is not None or '.' in name)):
                raise _Fallback()
            section = name if sub is None and '.' not in name else ''
        if not s or s[0] in '#;':
            continue
        m = _KEY.match(s.rstrip('\r'))
        if not m or section is None:
            raise _Fallback()               # git refuses the line, or a key comes before any section
        key, value = m.group(1).lower(), m.group(2)
        if value is not None:
            value = _value(value)
        if section == 'core' and key == 'worktree':
            raise _Fallback()
        out.append((section, key, value))
    return out


def _value(raw):
    """git's parse_value for the text after `=` on one line."""
    raw = raw[:-1] if raw.endswith('\r') else raw     # git reads \r\n as \n
    out, space, quote, i = [], 0, False, 0
    while i < len(raw):
        c = raw[i]
        i += 1
        if c in _SPACE and not quote:
            space += 1 if out else 0        # leading white space is dropped, inner kept
            continue
        if not quote and c in ';#':
            break                           # a comment runs to the end of the line
        if space:
            out.append(' ' * space)
            space = 0
        if c == '\\':
            if i == len(raw):
                raise _Fallback()           # continued onto the next line
            c = _ESCAPES.get(raw[i])
            if c is None:
                raise _Fallback()           # git refuses an unknown escape
            i += 1
            out.append(c)
        elif c == '"':
            quote = not quote
        else:
            out.append(c)
    if quote:
        raise _Fallback()                   # git refuses a quote left open
    return ''.join(out)


# ------------------------------------------------------------------ ownership (Windows)

_ME = []


def owned_by_me(path):
    """True when `path`'s owner is this process's user (git for Windows' ownership check passes
    without safe.directory). False when it cannot tell. Always True off Windows, where the
    fallback is not needed for the kit's machine (INFERRED: git's POSIX check compares st_uid)."""
    if os.name != 'nt':
        try:
            return os.stat(path).st_uid == os.geteuid()
        except (OSError, AttributeError):
            return False
    api = _win()
    if not _ME:
        token = api.wintypes.HANDLE()
        if not api.advapi.OpenProcessToken(api.kernel.GetCurrentProcess(), 0x0008, api.ctypes.byref(token)):
            return False
        try:
            need = api.wintypes.DWORD()
            api.advapi.GetTokenInformation(token, 1, None, 0, api.ctypes.byref(need))
            buf = api.ctypes.create_string_buffer(need.value)
            if not api.advapi.GetTokenInformation(token, 1, buf, need, api.ctypes.byref(need)):
                return False
        finally:
            api.kernel.CloseHandle(token)
        _ME.append(buf)                      # TOKEN_USER: the SID pointer comes first
    ctypes = api.ctypes
    sid_me = ctypes.c_void_p.from_buffer(_ME[0]).value
    owner, sd = ctypes.c_void_p(), ctypes.c_void_p()
    rc = api.advapi.GetNamedSecurityInfoW(path, 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(sd))
    if rc != 0:
        return False
    try:
        return bool(owner.value) and bool(api.advapi.EqualSid(owner, sid_me))
    finally:
        api.kernel.LocalFree(sd)


_API = []


def _win():
    """advapi32 and kernel32 with every signature this module calls declared (a HANDLE or a
    pointer passed as a bare int is truncated on 64-bit Windows)."""
    if _API:
        return _API[0]
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    vp, dw, h = ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE
    kernel.GetCurrentProcess.restype = h
    kernel.GetCurrentProcess.argtypes = []
    kernel.CloseHandle.argtypes = [h]
    kernel.LocalFree.argtypes = [vp]
    kernel.LocalFree.restype = vp
    advapi.OpenProcessToken.argtypes = [h, dw, ctypes.POINTER(h)]
    advapi.GetTokenInformation.argtypes = [h, ctypes.c_int, vp, dw, ctypes.POINTER(dw)]
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, dw, ctypes.POINTER(vp), vp, vp, vp,
                                             ctypes.POINTER(vp)]
    advapi.GetNamedSecurityInfoW.restype = dw
    advapi.EqualSid.argtypes = [vp, vp]

    class Api(object):
        pass
    api = Api()
    api.ctypes, api.wintypes, api.advapi, api.kernel = ctypes, wintypes, advapi, kernel
    _API.append(api)
    return api
