"""References: loose files and packed-refs in the common dir, HEAD and pseudo-refs per worktree.

Updates lock `<ref>.lock` exclusively as git does, so a fake and a real git (or two fakes) on one
repository exclude each other, and they fail with git's own words ("cannot lock ref"). No reflog
is written; a deleted ref's reflog is removed with it, as git does. A reflog read is Unsupported,
and so is reading a broken loose ref (one holding neither an object name nor "ref:": BrokenRef),
except in for-each-ref and `rev-parse --verify`, which warn "ignoring broken ref" as git does.
"""
import os
import re
import threading

from . import retry
from .errors import GitExit, Unsupported

_PACKED_CACHE = {}
_PACKED_LOCK = threading.Lock()

PER_WORKTREE_PREFIXES = ('refs/bisect/', 'refs/worktree/', 'refs/rewritten/')
_PSEUDO = re.compile(r'^[A-Z][A-Z0-9_]*$')
_HEXRE = re.compile(r'^[0-9a-f]{40}([0-9a-f]{24})?$')


class BrokenRef(Unsupported):
    """A loose ref file holding neither an object name nor "ref:". git warns "ignoring broken
    ref" at some readers and not others, so only the readers that catch it imitate one
    (for-each-ref through list_refs' `broken`, and `rev-parse --verify` without --quiet); to every
    other reader the fake does not model it."""

    def __init__(self, name):
        super().__init__('a broken ref (%s)' % name)
        self.name = name


def is_pseudo(name):
    return bool(_PSEUDO.match(name))


def is_per_worktree(name):
    return '/' not in name or name.startswith(PER_WORKTREE_PREFIXES)


def ref_dir(repo, name):
    """The directory holding the loose file for `name`."""
    if name.startswith(('main-worktree/', 'worktrees/')):
        raise Unsupported('ref %s' % name)
    return repo.gitdir if is_per_worktree(name) else repo.commondir


def loose_path(repo, name):
    return os.path.join(ref_dir(repo, name), name.replace('/', os.sep))


def check_refname(name, allow_onelevel=False):
    """True when `name` is a valid ref name (git check-ref-format rules)."""
    if not name or name == '@' or name.endswith('/') or name.endswith('.') or '@{' in name:
        return False
    if '..' in name or '//' in name or name.startswith('/'):
        return False
    for ch in name:
        if ord(ch) < 0x20 or ord(ch) == 0x7f or ch in ' ~^:?*[\\':
            return False
    comps = name.split('/')
    for c in comps:
        if not c or c.startswith('.') or c.endswith('.lock'):
            return False
    if len(comps) < 2 and not allow_onelevel:
        return False
    return True


def _read_file(path):
    """A ref file's text, None when there is none. Windows denies an open while another thread's
    _replace lands on the file, so that is retried as _replace retries its side."""
    began = retry.start()
    while True:
        try:
            with open(path, 'rb') as f:
                return f.read().decode('utf-8', 'replace')
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None
        except PermissionError as e:
            if os.path.isdir(path):
                return None
            if not retry.pause(began):
                raise retry.gave_up(e, began) from e


def packed(repo):
    """{name: (oid, peeled or None)} from packed-refs (cached by stat)."""
    path = os.path.join(repo.commondir, 'packed-refs')
    try:
        st = os.stat(path)
    except OSError:
        return {}
    # st_ino: on NTFS a replace gives the path a new one, so a same-size rewrite in the same clock
    # tick still misses. That is filesystem-dependent: where the id is reused or reported as 0 the
    # key degrades to mtime+size, and _forget_packed is the guard for _rewrite_packed's rewrite.
    key = (os.path.normcase(path), st.st_mtime_ns, st.st_size, st.st_ino)
    with _PACKED_LOCK:
        got = _PACKED_CACHE.get(key)
    if got is not None:
        return got
    out = {}
    text = _read_file(path) or ''
    last = None
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        if line.startswith('^'):
            if last is not None:
                out[last] = (out[last][0], line[1:].strip())
            continue
        oid, _, name = line.partition(' ')
        out[name.strip()] = (oid, None)
        last = name.strip()
    with _PACKED_LOCK:
        if len(_PACKED_CACHE) > 2000:
            _PACKED_CACHE.clear()
        _PACKED_CACHE[key] = out
    return out


def read_raw(repo, name):
    """('sym', target) / ('oid', hex) / None for one ref, not followed."""
    text = _read_file(loose_path(repo, name))
    if text is not None:
        t = text.strip()
        if t.startswith('ref:'):
            return ('sym', t[4:].strip())
        if name == 'FETCH_HEAD':
            t = t.split('\t', 1)[0].split()[0] if t else ''
        if _HEXRE.match(t[:repo.hexlen]) and len(t) >= repo.hexlen:
            return ('oid', t[:repo.hexlen])
        if name != 'FETCH_HEAD':
            raise BrokenRef(name)
        return None
    if not is_per_worktree(name):
        p = packed(repo).get(name)
        if p is not None:
            return ('oid', p[0])
    return None


def resolve(repo, name, depth=0):
    """(oid or None, the final ref name). A symref to a missing ref gives (None, target)."""
    if depth > 5:
        return None, name
    raw = read_raw(repo, name)
    if raw is None:
        return None, name
    if raw[0] == 'sym':
        return resolve(repo, raw[1], depth + 1)
    return raw[1], name


def head_target(repo):
    """The branch HEAD points at ('refs/heads/x'), or None when detached."""
    raw = read_raw(repo, 'HEAD')
    if raw and raw[0] == 'sym':
        return raw[1]
    return None


def peeled(repo, name):
    p = packed(repo).get(name)
    return p[1] if p else None


def _loose_names(base, prefix):
    out = []
    root = os.path.join(base, prefix.replace('/', os.sep)) if prefix else base
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, base).replace(os.sep, '/')
        for f in filenames:
            if f.endswith('.lock'):
                continue
            out.append(rel + '/' + f if rel != '.' else f)
    return out


def list_refs(repo, prefix='refs/', broken=None):
    """[(name, oid)] for every ref under `prefix` (which ends in '/'), sorted by name; symrefs are
    followed (a dangling one is left out). Per-worktree refs come from this worktree only.

    A broken loose ref raises BrokenRef, unless `broken` is a list: its name is then appended
    there and it is left out (for-each-ref's "ignoring broken ref")."""
    names = set()
    shared = [n for n in _loose_names(repo.commondir, 'refs') if n.startswith(prefix)]
    for n in shared:
        if not n.startswith(PER_WORKTREE_PREFIXES):
            names.add(n)
    if repo.linked:
        for n in _loose_names(repo.gitdir, 'refs'):
            if n.startswith(prefix) and n.startswith(PER_WORKTREE_PREFIXES):
                names.add(n)
    else:
        for n in shared:
            if n.startswith(PER_WORKTREE_PREFIXES):
                names.add(n)
    for n in packed(repo):
        if n.startswith(prefix):
            names.add(n)
    out = []
    for n in sorted(names, key=lambda s: s.encode('utf-8')):
        if not check_refname(n):
            continue
        try:
            oid, _ = resolve(repo, n)
        except BrokenRef:
            if broken is None:
                raise
            broken.append(n)
            continue
        if oid is not None:
            out.append((n, oid))
    return out


def lock_error(name, path, why='File exists'):
    p = path.replace('\\', '/')
    return GitExit(128, "fatal: cannot lock ref '%s': Unable to create '%s.lock': %s.\n\n"
                        "Another git process seems to be running in this repository, e.g.\n"
                        "an editor opened by 'git commit'. Please make sure all processes\n"
                        "are terminated then try again. If it still fails, a git process\n"
                        "may have crashed in this repository earlier:\n"
                        "remove the file manually to continue.\n" % (name, p, why))


class RefLock:
    def __init__(self, path, name):
        self.path = path
        self.name = name
        self.lock = path + '.lock'
        self.fd = None

    def acquire(self):
        d = os.path.dirname(self.path)
        try:
            os.makedirs(d, exist_ok=True)
        except (FileExistsError, NotADirectoryError):
            raise GitExit(128, "fatal: cannot lock ref '%s': unable to create directory for %s\n"
                          % (self.name, self.lock.replace('\\', '/')))
        # Windows denies creating a lock file another thread has just renamed or removed (it is
        # still being deleted); that window is retried, as _read_file retries its open.
        began = retry.start()
        while True:
            try:
                self.fd = os.open(self.lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o666)
                return
            except FileExistsError:
                raise lock_error(self.name, self.path)
            except PermissionError as e:
                if os.path.isdir(self.lock) or os.path.exists(self.lock):
                    raise lock_error(self.name, self.path)
                if not retry.pause(began):
                    raise retry.gave_up(e, began) from e

    def commit(self, data):
        """Write `data` and move the lock onto the ref. On any failure the lock file is removed,
        as git rolls back its lockfile: a lock left behind fails every later update of the ref
        ("cannot lock ref") until someone deletes it by hand. Once the lock has moved onto the ref
        nothing is rolled back: the lock path may by then be another writer's lock."""
        moved = False
        try:
            fd, self.fd = self.fd, None       # never closed twice, whatever fails below
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            if os.path.isdir(self.path):
                raise GitExit(128, "fatal: cannot lock ref '%s': there is a non-empty directory "
                                   "'%s' blocking reference '%s'\n"
                              % (self.name, self.path.replace('\\', '/'), self.name))
            _replace(self.lock, self.path)
            moved = True
        except BaseException:
            if not moved:
                self.rollback()
            raise

    def rollback(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            os.remove(self.lock)
        except OSError:
            pass


def _replace(src, dst):
    began = retry.start()
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            if not retry.pause(began):
                raise retry.gave_up(e, began) from e


def _df_conflict(repo, name, deleting=()):
    """The existing ref that blocks creating `name`, or None."""
    parts = name.split('/')
    pk = packed(repo)
    for i in range(1, len(parts)):
        anc = '/'.join(parts[:i])
        if anc in deleting:
            continue
        if not anc.startswith('refs/') and '/' not in anc:
            continue
        if os.path.isfile(loose_path(repo, anc)) or anc in pk:
            return anc
    pre = name + '/'
    for n in pk:
        if n.startswith(pre) and n not in deleting:
            return n
    d = loose_path(repo, name)
    if os.path.isdir(d):
        for dirpath, _dirs, files in os.walk(d):
            for f in files:
                if not f.endswith('.lock'):
                    full = os.path.relpath(os.path.join(dirpath, f), ref_dir(repo, name)).replace(os.sep, '/')
                    if full not in deleting:
                        return full
    return None


def update(repo, name, new, old=None, deref=True, msg=None, deleting=()):
    """Point ref `name` at oid `new`. `old`: None means no check, '' means it must not exist, an
    oid means it must be that. A symref is followed when `deref`. Raises GitExit on failure."""
    target = name
    if deref:
        raw = read_raw(repo, name)
        depth = 0
        while raw is not None and raw[0] == 'sym' and depth < 5:
            target = raw[1]
            raw = read_raw(repo, target)
            depth += 1
    if target != 'HEAD' and not is_pseudo(target) and not check_refname(target):
        raise GitExit(128, "fatal: update_ref failed for ref '%s': refusing to update ref with bad name '%s'\n"
                      % (name, target))
    if '/' in target:
        blocker = _df_conflict(repo, target, deleting)
        if blocker is not None:
            raise GitExit(128, "fatal: cannot lock ref '%s': '%s' exists; cannot create '%s'\n"
                          % (target, blocker, target))
    path = loose_path(repo, target)
    lk = RefLock(path, target)
    lk.acquire()
    try:
        cur = read_raw(repo, target)
        cur_oid = cur[1] if cur and cur[0] == 'oid' else None
        if old is not None:
            if old == '':
                if cur_oid is not None:
                    raise GitExit(128, "fatal: cannot lock ref '%s': reference already exists\n" % target)
            elif cur_oid is None:
                raise GitExit(128, "fatal: cannot lock ref '%s': unable to resolve reference '%s'\n"
                              % (target, target))
            elif cur_oid != old:
                raise GitExit(128, "fatal: cannot lock ref '%s': is at %s but expected %s\n"
                              % (target, cur_oid, old))
    except BaseException:
        lk.rollback()
        raise
    lk.commit(('%s\n' % new).encode('ascii'))
    return target


def write_symref(repo, name, target):
    path = loose_path(repo, name)
    lk = RefLock(path, name)
    lk.acquire()
    lk.commit(('ref: %s\n' % target).encode('utf-8'))


def delete(repo, name, old=None, deref=False):
    """Delete ref `name` (loose and packed). `old` as in update(). Returns False when it did not
    exist."""
    target = name
    if deref:
        raw = read_raw(repo, name)
        if raw is not None and raw[0] == 'sym':
            target = raw[1]
    path = loose_path(repo, target)
    lk = RefLock(path, target)
    lk.acquire()
    try:
        cur = read_raw(repo, target)
        if cur is None and not os.path.exists(path):
            if old not in (None, ''):
                raise GitExit(128, "fatal: cannot lock ref '%s': unable to resolve reference '%s'\n"
                              % (target, target))
            return False
        if old not in (None, '') and (cur is None or cur[1] != old):
            raise GitExit(128, "fatal: cannot lock ref '%s': is at %s but expected %s\n"
                          % (target, cur[1] if cur else '0' * repo.hexlen, old))
        if target in packed(repo):
            _rewrite_packed(repo, drop={target})
        try:
            if not os.path.isdir(path):
                os.remove(path)
        except FileNotFoundError:
            pass
        _remove_reflog(repo, target)
    finally:
        lk.rollback()
    _prune_dirs(repo, target)
    return True


def _remove_reflog(repo, name):
    log = os.path.join(repo.gitdir if is_per_worktree(name) else repo.commondir, 'logs',
                       name.replace('/', os.sep))
    try:
        os.remove(log)
    except OSError:
        pass
    base = os.path.join(repo.gitdir if is_per_worktree(name) else repo.commondir, 'logs', 'refs')
    d = os.path.dirname(log)
    while os.path.normcase(d) != os.path.normcase(base) and len(d) > len(base):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


def _prune_dirs(repo, name):
    parts = name.split('/')
    base = ref_dir(repo, name)
    for i in range(len(parts) - 1, 1, -1):
        d = os.path.join(base, *parts[:i])
        try:
            os.rmdir(d)
        except OSError:
            break


def _rewrite_packed(repo, drop):
    path = os.path.join(repo.commondir, 'packed-refs')
    lk = RefLock(path, 'packed-refs')
    try:
        lk.acquire()
    except GitExit:
        raise GitExit(128, "error: Unable to create '%s.lock': File exists.\n" % path.replace('\\', '/'))
    text = _read_file(path) or ''
    out = []
    skip = False
    for line in text.splitlines(True):
        if line.startswith('^'):
            if not skip:
                out.append(line)
            continue
        if line.startswith('#'):
            out.append(line)
            skip = False
            continue
        name = line.rstrip('\n').partition(' ')[2]
        skip = name in drop
        if not skip:
            out.append(line)
    lk.commit(''.join(out).encode('utf-8'))
    _forget_packed(path)


def _forget_packed(path):
    norm = os.path.normcase(path)
    with _PACKED_LOCK:
        for k in [k for k in _PACKED_CACHE if k[0] == norm]:
            del _PACKED_CACHE[k]


def rename(repo, old, new):
    """Move ref `old` to `new` (branch -m)."""
    oid, _ = resolve(repo, old)
    if oid is None:
        raise GitExit(128, "fatal: no branch named '%s'\n" % old)
    delete(repo, old)
    update(repo, new, oid, old='')


def exists(repo, name):
    return read_raw(repo, name) is not None


def dwim(repo, short):
    """Every full ref name `short` can mean, in git's rule order."""
    rules = ('%s', 'refs/%s', 'refs/tags/%s', 'refs/heads/%s', 'refs/remotes/%s', 'refs/remotes/%s/HEAD')
    out = []
    for r in rules:
        full = r % short
        if r == '%s' and not (short.startswith('refs/') or is_pseudo(short)):
            continue
        if r == '%s' and not short.startswith('refs/') and short != 'HEAD':
            # a pseudo-ref other than HEAD lives only in the git dir
            pass
        if not check_refname(full, allow_onelevel=True) and not is_pseudo(full):
            continue
        oid, _ = resolve(repo, full)
        if oid is not None:
            out.append(full)
    return out
