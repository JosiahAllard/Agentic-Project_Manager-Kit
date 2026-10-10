"""The work tree: line-ending conversion (convert.c), hashing files as git would store them,
comparing the index with the files (read-cache.c ie_match_stat, racy-git rules), the untracked and
ignored walk (dir.c), and writing or removing files for a checkout (unpack-trees.c twoway_merge).

Conversion follows core.autocrlf only: any .gitattributes (worktree root, a tracked one, or one
met during a walk), $GIT_DIR/info/attributes or a core.attributesFile that exists is Unsupported.
core.filemode false (Git for Windows) keeps the index's mode; a symlink entry is Unsupported.
"""
import os
import stat as _stat

from . import ignore as _ignore
from .errors import GitExit, Unsupported
from .index import MODE_EXEC, MODE_FILE, S_IFLNK, Entry
from .objects import hash_object

# -- line endings ---------------------------------------------------------------------------


class Stats:
    __slots__ = ('nul', 'lonecr', 'lonelf', 'crlf', 'printable', 'nonprintable')


def gather_stats(data):
    s = Stats()
    s.nul = data.count(b'\0')
    s.crlf = data.count(b'\r\n')
    cr = data.count(b'\r')
    lf = data.count(b'\n')
    s.lonecr = cr - s.crlf
    s.lonelf = lf - s.crlf
    nonprint = 0
    for b in (127,) + tuple(range(0, 32)):
        if b in (8, 9, 27, 12, 10, 13):          # \b \t \033 \014 printable; CR/LF counted above
            continue
        if b == 0:
            nonprint += s.nul
            continue
        if b == 127:
            nonprint += data.count(b'\x7f')
            continue
        nonprint += data.count(bytes((b,)))
    if data.endswith(b'\x1a'):
        nonprint -= 1
    s.nonprintable = nonprint
    s.printable = len(data) - nonprint - s.nul + (1 if data.endswith(b'\x1a') else 0)
    # CR and LF are neither: git counts them before the printable test.
    s.printable -= cr + lf - s.crlf
    return s


def is_binary(s):
    return bool(s.lonecr or s.nul or (s.printable >> 7) < s.nonprintable)


def _will_lf_to_crlf(s):
    if s.lonelf == 0:
        return False
    if s.lonecr or s.crlf:
        return False
    return not is_binary(s)


class Convert:
    """Line-ending rules of one repository (core.autocrlf: 'true', 'input' or None)."""

    def __init__(self, repo):
        self.repo = repo
        v = repo.config.get('core.autocrlf')
        if v is None:
            self.mode = None
        elif v.strip().lower() == 'input':
            self.mode = 'input'
        else:
            from .config import parse_bool
            self.mode = 'true' if parse_bool(v, 'core.autocrlf') else None
        self._checked = False

    def guard(self, index=None):
        """Unsupported when attributes could change conversion."""
        if not self._checked:
            self._checked = True
            repo = self.repo
            cand = [os.path.join(repo.commondir, 'info', 'attributes')]
            if repo.worktree:
                cand.append(os.path.join(repo.worktree, '.gitattributes'))
            af = repo.config.get('core.attributesfile')
            if af:
                cand.append(os.path.expanduser(af))
            else:
                xdg = repo.env.get('XDG_CONFIG_HOME')
                home = repo.env.get('HOME') or repo.env.get('USERPROFILE') or os.path.expanduser('~')
                cand.append(os.path.join(xdg, 'git', 'attributes') if xdg
                            else os.path.join(home, '.config', 'git', 'attributes'))
            for p in cand:
                if os.path.isfile(p):
                    raise Unsupported('git attributes (%s)' % p.replace('\\', '/'))
        if index is not None:
            for (p, _s) in index.entries:
                if p == '.gitattributes' or p.endswith('/.gitattributes'):
                    raise Unsupported('a tracked .gitattributes (%s)' % p)

    def to_git(self, path, data, index_has_cr=None, warn=None):
        """`data` as git stores it. index_has_cr() says whether the index blob holds a CR; warn
        gets git's safecrlf warning text."""
        if self.mode is None or not data:
            return data
        s = gather_stats(data)
        convert = bool(s.crlf)
        if is_binary(s):
            return data
        if convert and index_has_cr is not None and index_has_cr():
            convert = False
        if warn is not None:
            new_crlf, new_lonelf = s.crlf, s.lonelf
            if convert:
                new_lonelf += new_crlf
                new_crlf = 0
            if self.mode == 'true':
                t = Stats()
                t.nul, t.lonecr, t.printable, t.nonprintable = s.nul, s.lonecr, s.printable, s.nonprintable
                t.crlf, t.lonelf = new_crlf, new_lonelf
                if _will_lf_to_crlf(t):
                    new_crlf += new_lonelf
                    new_lonelf = 0
            if s.crlf and not new_crlf:
                warn("warning: in the working copy of '%s', CRLF will be replaced by LF the next "
                     "time Git touches it\n" % path)
            elif s.lonelf and not new_lonelf:
                warn("warning: in the working copy of '%s', LF will be replaced by CRLF the next "
                     "time Git touches it\n" % path)
        if not convert:
            return data
        return data.replace(b'\r\n', b'\n')

    def to_worktree(self, data):
        if self.mode != 'true' or not data:
            return data
        s = gather_stats(data)
        if not _will_lf_to_crlf(s):
            return data
        return data.replace(b'\n', b'\r\n')


# -- reading files --------------------------------------------------------------------------


def lstat(repo, path):
    try:
        return os.lstat(repo.wt_path(path))
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return None


def read_file(repo, path):
    with open(repo.wt_path(path), 'rb') as f:
        return f.read()


def is_reg(st):
    return st is not None and _stat.S_ISREG(st.st_mode)


def wt_mode(repo, st, entry_mode=None):
    """The index mode for a file with stat `st` (core.filemode false keeps the old mode)."""
    if _stat.S_ISLNK(st.st_mode):
        raise Unsupported('symbolic link in the work tree')
    if not repo.config.bool('core.filemode', False):
        return entry_mode if entry_mode in (MODE_FILE, MODE_EXEC) else MODE_FILE
    return MODE_EXEC if st.st_mode & 0o100 else MODE_FILE


class Tree:
    """Work-tree operations for one command: shared Convert, index blob lookups."""

    def __init__(self, repo, index):
        self.repo = repo
        self.index = index
        self.conv = Convert(repo)
        self.conv.guard(index)
        self.warnings = []

    def _index_has_cr(self, path):
        def f():
            e = self.index.get(path)
            if e is None:
                return False
            got = self.repo.objects.read(e.oid)
            return got is not None and b'\r' in got[1]
        return f

    def blob_data(self, path, warn=False):
        data = read_file(self.repo, path)
        return self.conv.to_git(path, data, self._index_has_cr(path),
                                self.warnings.append if warn else None)

    def hash_path(self, path, write=False, warn=False):
        data = self.blob_data(path, warn)
        if write:
            return self.repo.objects.write('blob', data)
        return hash_object(self.repo.algo, 'blob', data)

    def entry_for(self, path, write=True, warn=False):
        """A fresh stage-0 Entry for the file at `path` (blob written when `write`)."""
        st = lstat(self.repo, path)
        if st is None or not is_reg(st):
            if st is not None and _stat.S_ISLNK(st.st_mode):
                raise Unsupported('symbolic link in the work tree: %s' % path)
            return None
        old = self.index.get(path)
        mode = wt_mode(self.repo, st, old.mode if old else None)
        oid = self.hash_path(path, write=write, warn=warn)
        e = Entry(path, mode, oid)
        e.set_stat(st)
        return e

    def modified(self, e, racy_mtime=None):
        """(changed?, st or None). Compares stat data, then content (racily clean or stat
        differs). A missing file counts as changed with st None."""
        if e.mode == S_IFLNK:
            raise Unsupported('symlink entry %s' % e.path)
        st = lstat(self.repo, e.path)
        if st is None or not is_reg(st):
            if st is not None and _stat.S_ISLNK(st.st_mode):
                raise Unsupported('symbolic link in the work tree: %s' % e.path)
            return True, None
        if self.repo.config.bool('core.filemode', False):
            if wt_mode(self.repo, st, e.mode) != e.mode:
                return True, st
        if e.stat_matches(st):
            racy = racy_mtime is not None and e.mtime >= racy_mtime
            if not racy:
                return False, st
        if e.size and (st.st_size & 0xffffffff) != e.size and self.conv.mode is None:
            return True, st
        return self.hash_path(e.path) != e.oid, st

    def refresh(self, report=None):
        """Bring stat data up to date for clean entries; `report(path, why)` for others.
        True when the index changed. ctime counts only under core.trustctime (git's default
        true), as in git's refresh: the fixture sets it false, so a copied repository's new
        creation times leave the index bytes alone."""
        changed = False
        racy = self.index.mtime_ns
        trust_ctime = self.repo.config.bool('core.trustctime', True)
        seen_unmerged = set()
        for (path, stage), e in sorted(self.index.entries.items(), key=lambda kv: (kv[0][0].encode('utf-8'), kv[0][1])):
            if stage:
                if path not in seen_unmerged:
                    seen_unmerged.add(path)
                    if report:
                        report(path, 'needs merge')
                continue
            if e.intent_to_add:
                continue
            mod, st = self.modified(e, racy)
            if mod:
                if report:
                    report(path, 'needs update')
                continue
            if (e.mtime != st.st_mtime_ns or e.size != (st.st_size & 0xffffffff)
                    or (trust_ctime and e.ctime != _ctime(st))
                    or (racy is not None and e.mtime >= racy)):
                e.set_stat(st)
                changed = True
        return changed

    # -- writing --
    def write_blob(self, path, oid, mode):
        """Write blob `oid` at `path` (dirs made, a file or empty dir in the way removed);
        returns a fresh Entry."""
        if mode == S_IFLNK:
            raise Unsupported('checking out a symlink (%s)' % path)
        got = self.repo.objects.read(oid)
        if got is None:
            raise GitExit(128, 'fatal: unable to read %s\n' % oid)
        data = self.conv.to_worktree(got[1])
        full = self.repo.wt_path(path)
        parent = os.path.dirname(full)
        self._make_dirs(path)
        if os.path.isdir(full) and not os.path.islink(full):
            _remove_tree_if_expendable(full)
        try:
            os.remove(full)
        except FileNotFoundError:
            pass
        except PermissionError:
            os.chmod(full, _stat.S_IWRITE)
            os.remove(full)
        with open(full, 'wb') as f:
            f.write(data)
        st = os.lstat(full)
        e = Entry(path, mode, oid)
        e.set_stat(st)
        return e

    def _make_dirs(self, path):
        parts = path.split('/')[:-1]
        cur = self.repo.worktree
        for p in parts:
            cur = os.path.join(cur, p)
            if os.path.isdir(cur):
                continue
            if os.path.lexists(cur):
                os.remove(cur)
            os.mkdir(cur)

    def remove_file(self, path):
        full = self.repo.wt_path(path)
        try:
            os.remove(full)
        except FileNotFoundError:
            pass
        except IsADirectoryError:
            return
        except PermissionError:
            if os.path.isdir(full):
                return
            os.chmod(full, _stat.S_IWRITE)
            os.remove(full)
        self.prune_dirs(path)

    def prune_dirs(self, path):
        parts = path.split('/')[:-1]
        while parts:
            d = os.path.join(self.repo.worktree, *parts)
            try:
                os.rmdir(d)
            except OSError:
                break
            parts.pop()


def _ctime(st):
    b = getattr(st, 'st_birthtime_ns', None)
    return b if b is not None else st.st_ctime_ns


def _remove_tree_if_expendable(full):
    for dirpath, dirs, files in os.walk(full):
        if files:
            raise Unsupported('a directory with files where a file is checked out (%s)' % full)
    for dirpath, dirs, files in os.walk(full, topdown=False):
        os.rmdir(dirpath)


# -- the untracked walk ---------------------------------------------------------------------


class Walk:
    """dir.c's fill_directory for one index.

    untracked: 'no', 'normal' (an untracked directory collapses to 'dir/'), 'all'.
    ignored: None, 'traditional' or 'matching' (status --ignored).
    Results (untracked, ignored) are lists of '/'-paths, a directory ending in '/'."""

    def __init__(self, repo, index, untracked='normal', ignored=None, conv=None):
        self.repo = repo
        self.index = index
        self.mode = untracked
        self.ignored_mode = ignored
        self.ign = _ignore.Ignore(repo)
        self.icase = repo.config.bool('core.ignorecase', False)
        self.conv = conv
        files, dirs = set(), set()
        for (p, _s) in index.entries:
            k = p.lower() if self.icase else p
            files.add(k)
            parts = k.split('/')
            for i in range(1, len(parts)):
                dirs.add('/'.join(parts[:i]))
        self.tracked_files, self.tracked_dirs = files, dirs

    def _key(self, p):
        return p.lower() if self.icase else p

    def run(self, limit=None):
        """limit: a list of '/'-path prefixes (a pathspec of plain paths); None means all."""
        untracked, ignored = [], []
        self._walk('', untracked, ignored)
        if limit is not None:
            def keep(p):
                for l in limit:
                    l = l.rstrip('/')
                    if l in ('', '.'):
                        return True
                    if p == l or p.startswith(l + '/') or (p.endswith('/') and (l + '/').startswith(p)):
                        return True
                return False
            untracked = [p for p in untracked if keep(p)]
            ignored = [p for p in ignored if keep(p)]
        return untracked, ignored

    def _entries(self, rel):
        full = os.path.join(self.repo.worktree, rel.replace('/', os.sep)) if rel else self.repo.worktree
        try:
            it = list(os.scandir(full))
        except OSError:
            return []
        out = []
        for de in it:
            name = de.name
            if rel == '' and name.lower() == '.git' or name == '.git':
                continue
            try:
                if de.is_symlink():
                    raise Unsupported('symbolic link in the work tree: %s%s' % (rel, name))
                isdir = de.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if name == '.gitattributes':
                raise Unsupported('a .gitattributes in the work tree (%s%s)' % (rel, name))
            out.append((name, isdir))
        out.sort(key=lambda t: (t[0] + ('/' if t[1] else '')).encode('utf-8', 'surrogateescape'))
        return out

    def _walk(self, rel, untracked, ignored):
        """Walk a tracked (or the top) directory."""
        for name, isdir in self._entries(rel):
            path = rel + name
            key = self._key(path)
            if not isdir:
                if key in self.tracked_files:
                    continue
                self._file(path, untracked, ignored)
                continue
            if key in self.tracked_files:
                continue                     # a directory where the index has a file: shown as deleted
            if key in self.tracked_dirs:
                if _nested_repo(self.repo, path):
                    continue
                self._walk(path + '/', untracked, ignored)
                continue
            self._untracked_dir(path, untracked, ignored)

    def _file(self, path, untracked, ignored):
        if self.ign.is_ignored(path, False):
            if self.ignored_mode:
                ignored.append(path)
            return
        if self.mode != 'no':
            untracked.append(path)

    def _untracked_dir(self, path, untracked, ignored):
        """A directory with nothing tracked under it."""
        if _nested_repo(self.repo, path):
            if self.ign.is_ignored(path, True):
                if self.ignored_mode:
                    ignored.append(path + '/')
                return
            if self.mode != 'no':
                untracked.append(path + '/')
            return
        if self.ign.is_ignored(path, True):
            if not self.ignored_mode:
                return
            if self.ignored_mode == 'matching' or self.mode != 'all':
                if self._has_files(path):
                    ignored.append(path + '/')
                return
            # traditional with -uall: the files one by one
            sub_u, sub_i = [], []
            self._collect(path + '/', sub_u, sub_i, force_ignored=True)
            ignored.extend(sub_i)
            return
        sub_u, sub_i = [], []
        self._collect(path + '/', sub_u, sub_i)
        if self.mode == 'all':
            untracked.extend(sub_u)
            if self.ignored_mode:
                ignored.extend(sub_i)
            return
        if sub_u:
            if self.mode != 'no':
                untracked.append(path + '/')
            # traditional shows ignored files inside an untracked directory one by one
            if self.ignored_mode == 'traditional':
                ignored.extend(sub_i)
            elif self.ignored_mode == 'matching':
                ignored.extend(sub_i)
            return
        if sub_i and self.ignored_mode:
            if self.ignored_mode == 'traditional':
                ignored.append(path + '/')
            else:
                ignored.extend(sub_i)

    def _collect(self, rel, untracked, ignored, force_ignored=False):
        """Every file under untracked directory `rel` (ignored ones apart); nested repos and
        ignored directories as 'dir/' (matching mode) or expanded."""
        for name, isdir in self._entries(rel):
            path = rel + name
            if not isdir:
                if force_ignored or self.ign.is_ignored(path, False):
                    ignored.append(path)
                else:
                    untracked.append(path)
                continue
            if _nested_repo(self.repo, path):
                if force_ignored or self.ign.is_ignored(path, True):
                    ignored.append(path + '/')
                else:
                    untracked.append(path + '/')
                continue
            if force_ignored:
                self._collect(path + '/', untracked, ignored, True)
                continue
            if self.ign.is_ignored(path, True):
                if self.ignored_mode == 'matching' or (self.ignored_mode and self.mode != 'all'):
                    if self._has_files(path):
                        ignored.append(path + '/')
                elif self.ignored_mode:
                    self._collect(path + '/', untracked, ignored, True)
                continue
            self._collect(path + '/', untracked, ignored)

    def _has_files(self, path):
        full = self.repo.wt_path(path)
        for _dp, _dirs, files in os.walk(full):
            if files:
                return True
        return False


def _nested_repo(repo, path):
    p = os.path.join(repo.wt_path(path), '.git')
    return os.path.exists(p)


# -- two-way checkout -----------------------------------------------------------------------


def _same(a, b):
    if a is None or b is None:
        return a is None and b is None
    return a[0] == b[0] and a[1] == b[1]


CHECKOUT_MSGS = {
    'overwrite': ('Your local changes to the following files would be overwritten by %s:\n%s'
                  'Please commit your changes or stash them before you %s.'),
    'untracked': ('The following untracked working tree files would be overwritten by %s:\n%s'
                  'Please move or remove them before you %s.'),
    'removed': ('The following untracked working tree files would be removed by %s:\n%s'
                'Please move or remove them before you %s.'),
}


def rejected_error(rejects, cmd='checkout', action='switch branches'):
    """GitExit with git's display_error_msgs text for {kind: [paths]}."""
    parts = []
    for kind in ('overwrite', 'untracked', 'removed'):
        paths = rejects.get(kind)
        if not paths:
            continue
        listed = ''.join('\t%s\n' % p for p in sorted(set(paths), key=lambda s: s.encode('utf-8')))
        parts.append('error: ' + CHECKOUT_MSGS[kind] % (cmd, listed, action) + '\n')
    return GitExit(1, ''.join(parts) + 'Aborting\n')


def twoway(tree, old, new, reset=False, cmd='checkout', action='switch branches', errfn=None):
    """Move the index and work tree from flat tree `old` to flat tree `new` ({path: (mode, oid)},
    modes as ints), keeping local changes as git's twoway_merge does. Raises GitExit with git's
    message when a change would be lost (`errfn(rejects)` builds it when given). Returns nothing;
    tree.index is updated (not written)."""
    repo, index = tree.repo, tree.index
    paths = set(old) | set(new) | {p for (p, _s) in index.entries}
    plan = []                  # (path, action, new entry tuple)
    rejects = {}
    racy = index.mtime_ns
    ign = None
    for path in sorted(paths, key=lambda s: s.encode('utf-8')):
        o, n = old.get(path), new.get(path)
        stages = index.stages(path)
        cur_e = stages.get(0)
        conflicted = bool(stages) and cur_e is None
        cur = (cur_e.mode, cur_e.oid) if cur_e is not None else None
        if reset:
            if n is None:
                if stages:
                    plan.append((path, 'delete', None))
            elif cur is None or not _same(cur, n):
                plan.append((path, 'write', n))
            else:
                mod, _st = tree.modified(cur_e, racy)
                if mod:
                    plan.append((path, 'write', n))
            continue
        if conflicted:
            if _same(o, n):
                plan.append((path, 'delete' if n is None else 'write', n))
                continue
            rejects.setdefault('overwrite', []).append(path)
            continue
        if cur is not None:
            if ((o is None and n is None) or (o is None and _same(cur, n)) or
                    (o is not None and n is not None and _same(o, n)) or
                    (o is not None and n is not None and _same(cur, n))):
                continue                                         # keep
            if o is not None and n is None and _same(cur, o):
                if _uptodate(tree, cur_e, racy):
                    plan.append((path, 'delete', None))
                else:
                    rejects.setdefault('overwrite', []).append(path)
                continue
            if o is not None and n is not None and _same(cur, o):
                if _uptodate(tree, cur_e, racy):
                    plan.append((path, 'write', n))
                else:
                    rejects.setdefault('overwrite', []).append(path)
                continue
            rejects.setdefault('overwrite', []).append(path)
            continue
        if n is not None:
            if o is not None:
                if _same(o, n):
                    continue
                rejects.setdefault('overwrite', []).append(path)
                continue
            st = lstat(repo, path)
            if st is not None:
                if ign is None:
                    ign = _ignore.Ignore(repo)
                if _stat.S_ISDIR(st.st_mode):
                    if _dir_has_files(repo.wt_path(path)):
                        raise Unsupported('a directory in the way of %s' % path)
                elif not ign.is_ignored(path, False):
                    rejects.setdefault('untracked', []).append(path)
                    continue
            else:
                blocker = _file_in_the_way(repo, path, index)
                if blocker:
                    if ign is None:
                        ign = _ignore.Ignore(repo)
                    if not ign.is_ignored(blocker, False):
                        rejects.setdefault('untracked', []).append(blocker)
                        continue
            plan.append((path, 'write', n))
            continue
        # neither in the index nor in new: nothing to do
    if rejects:
        if errfn is not None:
            raise errfn(rejects)
        raise rejected_error(rejects, cmd, action)
    for path, act, n in plan:
        if act == 'delete':
            index.remove(path)
            tree.remove_file(path)
    for path, act, n in plan:
        if act == 'write':
            e = tree.write_blob(path, n[1], n[0])
            index.remove(path)
            index.add(e)


def _uptodate(tree, e, racy):
    st = lstat(tree.repo, e.path)
    if st is None:
        return True
    mod, _ = tree.modified(e, racy)
    return not mod


def _dir_has_files(full):
    for _dp, _d, files in os.walk(full):
        if files:
            return True
    return False


def _file_in_the_way(repo, path, index):
    """An untracked file at a parent path of `path` ('a' blocks 'a/b'), or None."""
    parts = path.split('/')
    for i in range(1, len(parts)):
        anc = '/'.join(parts[:i])
        full = repo.wt_path(anc)
        if os.path.isfile(full) and index.get(anc) is None:
            return anc
    return None
