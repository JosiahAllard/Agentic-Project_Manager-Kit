"""The index file: read versions 2-4, write version 2 (3 when an entry needs extended flags).

Extensions are dropped on write (the cache tree and untracked cache are optional; git rebuilds
them). A split index or sparse index is Unsupported. Stat data is recorded as Git for Windows
records it (ctime is the creation time; dev, ino, uid and gid are 0). A recent entry whose stat
data matches a file with other content is smudged (size 0), as git does, so a later reader checks
its content; a clean entry keeps its size, which real git trusts without refreshing.
"""
import hashlib
import os
import struct
import time

from . import retry
from .errors import GitExit, Unsupported

S_IFREG = 0o100000
S_IFLNK = 0o120000
S_IFGITLINK = 0o160000
MODE_FILE = 0o100644
MODE_EXEC = 0o100755


class Entry:
    __slots__ = ('path', 'mode', 'oid', 'stage', 'ctime', 'mtime', 'dev', 'ino', 'uid', 'gid', 'size',
                 'assume_valid', 'skip_worktree', 'intent_to_add')

    def __init__(self, path, mode, oid, stage=0, ctime=0, mtime=0, size=0):
        self.path = path            # str, '/'-separated
        self.mode = mode
        self.oid = oid
        self.stage = stage
        self.ctime = ctime          # ns
        self.mtime = mtime          # ns
        self.dev = self.ino = self.uid = self.gid = 0
        self.size = size
        self.assume_valid = self.skip_worktree = self.intent_to_add = False

    def copy(self):
        e = Entry(self.path, self.mode, self.oid, self.stage, self.ctime, self.mtime, self.size)
        e.dev, e.ino, e.uid, e.gid = self.dev, self.ino, self.uid, self.gid
        e.assume_valid, e.skip_worktree, e.intent_to_add = self.assume_valid, self.skip_worktree, self.intent_to_add
        return e

    def set_stat(self, st):
        self.ctime = _ctime_ns(st)
        self.mtime = st.st_mtime_ns
        self.size = st.st_size & 0xffffffff
        self.dev = self.ino = self.uid = self.gid = 0

    def stat_matches(self, st):
        return (self.mtime == st.st_mtime_ns and self.size == (st.st_size & 0xffffffff)
                and self.size != 0)


def _ctime_ns(st):
    b = getattr(st, 'st_birthtime_ns', None)
    return b if b is not None else st.st_ctime_ns


def path_key(path):
    return path.encode('utf-8')


class Index:
    def __init__(self, repo, path=None):
        self.repo = repo
        self.path = path or repo.index_path
        self.entries = {}           # (path, stage) -> Entry
        self.version = 2
        self.mtime_ns = None
        self.exists = False

    # -- reading --
    def load(self):
        try:
            with open(self.path, 'rb') as f:
                data = f.read()
                st = os.fstat(f.fileno())
        except FileNotFoundError:
            return self
        self.exists = True
        self.mtime_ns = st.st_mtime_ns
        self._parse(data)
        return self

    def _parse(self, data):
        hashlen = self.repo.hexlen // 2
        if len(data) < 12 + hashlen or data[:4] != b'DIRC':
            raise GitExit(128, 'fatal: index file corrupt\n')
        version, count = struct.unpack('>II', data[4:12])
        if version not in (2, 3, 4):
            raise GitExit(128, 'fatal: index file corrupt\n')
        self.version = version
        pos = 12
        prev = b''
        for _ in range(count):
            start = pos
            (cs, cn, ms, mn, dev, ino, mode, uid, gid, size) = struct.unpack('>10I', data[pos:pos + 40])
            pos += 40
            oid = data[pos:pos + hashlen].hex()
            pos += hashlen
            flags, = struct.unpack('>H', data[pos:pos + 2])
            pos += 2
            ext = 0
            if flags & 0x4000:
                if version < 3:
                    raise GitExit(128, 'fatal: index file corrupt\n')
                ext, = struct.unpack('>H', data[pos:pos + 2])
                pos += 2
            namelen = flags & 0xfff
            if version == 4:
                strip = 0
                shift = 0
                # offset varint (git's encode_varint)
                c = data[pos]
                pos += 1
                strip = c & 0x7f
                while c & 0x80:
                    c = data[pos]
                    pos += 1
                    strip = ((strip + 1) << 7) | (c & 0x7f)
                nul = data.index(b'\0', pos)
                name = prev[:len(prev) - strip] + data[pos:nul]
                pos = nul + 1
            else:
                if namelen < 0xfff:
                    name = data[pos:pos + namelen]
                    nul = pos + namelen
                else:
                    nul = data.index(b'\0', pos)
                    name = data[pos:nul]
                entry_len = (nul - start) + 8 - ((nul - start) % 8)
                pos = start + entry_len
            prev = name
            e = Entry(name.decode('utf-8', 'surrogateescape'), mode, oid, (flags >> 12) & 3,
                      cs * 1000000000 + cn, ms * 1000000000 + mn, size)
            e.dev, e.ino, e.uid, e.gid = dev, ino, uid, gid
            e.assume_valid = bool(flags & 0x8000)
            e.skip_worktree = bool(ext & 0x4000)
            e.intent_to_add = bool(ext & 0x2000)
            if e.skip_worktree:
                raise Unsupported('skip-worktree entries (sparse checkout)')
            if mode == S_IFGITLINK:
                raise Unsupported('gitlink (submodule) in the index')
            self.entries[(e.path, e.stage)] = e
        end = len(data) - hashlen
        while pos + 8 <= end:
            sig = data[pos:pos + 4]
            size, = struct.unpack('>I', data[pos + 4:pos + 8])
            if sig in (b'link', b'sdir'):
                raise Unsupported('index extension %s' % sig.decode('ascii', 'replace'))
            if not (65 <= sig[0] <= 90):
                raise Unsupported('required index extension %r' % sig)
            pos += 8 + size

    # -- queries --
    def get(self, path, stage=0):
        return self.entries.get((path, stage))

    def sorted_entries(self):
        return sorted(self.entries.values(), key=lambda e: (path_key(e.path), e.stage))

    def paths(self):
        return sorted({p for p, _ in self.entries}, key=path_key)

    def unmerged(self):
        return sorted({p for p, s in self.entries if s}, key=path_key)

    def stages(self, path):
        return {s: e for (p, s), e in self.entries.items() if p == path}

    def has_path(self, path):
        return any((path, s) in self.entries for s in (0, 1, 2, 3))

    # -- changes --
    def add(self, entry):
        """Put a stage-0 entry, dropping any conflict stages and D/F clashes for its path."""
        for s in (1, 2, 3):
            self.entries.pop((entry.path, s), None)
        # a file replaces a directory of that name and vice versa
        pre = entry.path + '/'
        for key in [k for k in self.entries if k[0].startswith(pre)]:
            del self.entries[key]
        parts = entry.path.split('/')
        for i in range(1, len(parts)):
            anc = '/'.join(parts[:i])
            for s in (0, 1, 2, 3):
                self.entries.pop((anc, s), None)
        self.entries[(entry.path, entry.stage)] = entry

    def remove(self, path):
        for s in (0, 1, 2, 3):
            self.entries.pop((path, s), None)

    def clear(self):
        self.entries = {}

    # -- writing --
    def write(self, lock=None):
        own = lock is None
        if own:
            lock = IndexLock(self.path)
            lock.acquire()
        try:
            data = self.serialize()
            lock.commit(data)
        except BaseException:
            if own:
                lock.rollback()
            raise

    def serialize(self):
        hashlen = self.repo.hexlen // 2
        now_ns = time.time_ns()
        entries = self.sorted_entries()
        ext = any(e.skip_worktree or e.intent_to_add for e in entries)
        version = 3 if ext else 2
        out = [b'DIRC', struct.pack('>II', version, len(entries))]
        tree = []
        for e in entries:
            size = e.size
            if e.mtime >= now_ns - 2_000_000_000 and self._racily_modified(e, tree):
                size = 0                                 # stat says clean, content not: smudge
            name = e.path.encode('utf-8', 'surrogateescape')
            flags = (min(len(name), 0xfff)) | (e.stage << 12) | (0x8000 if e.assume_valid else 0)
            extflags = 0
            if e.skip_worktree:
                extflags |= 0x4000
            if e.intent_to_add:
                extflags |= 0x2000
            if extflags:
                flags |= 0x4000
            rec = struct.pack('>10I', (e.ctime // 1000000000) & 0xffffffff, e.ctime % 1000000000,
                              (e.mtime // 1000000000) & 0xffffffff, e.mtime % 1000000000,
                              e.dev & 0xffffffff, e.ino & 0xffffffff, e.mode, e.uid, e.gid,
                              size & 0xffffffff)
            rec += bytes.fromhex(e.oid) + struct.pack('>H', flags)
            if extflags:
                rec += struct.pack('>H', extflags)
            rec += name
            pad = 8 - (len(rec) % 8)
            rec += b'\0' * pad
            out.append(rec)
        body = b''.join(out)
        h = hashlib.new(self.repo.algo)
        h.update(body)
        return body + h.digest()[:hashlen]

    def _racily_modified(self, e, tree):
        """git's ce_smudge_racily_clean_entry: True when the file's stat data still matches the
        entry but its content does not. Only such an entry is smudged; a clean one keeps its
        size, which a later real git trusts without reading the file (`tree` caches a wt.Tree)."""
        if (self.repo.worktree is None or e.stage or e.size == 0
                or e.mode not in (MODE_FILE, MODE_EXEC)):
            return False
        from . import wt as _wt
        st = _wt.lstat(self.repo, e.path)
        if st is None or not _wt.is_reg(st) or not e.stat_matches(st):
            return False
        if not tree:
            tree.append(_wt.Tree(self.repo, self))
        return tree[0].hash_path(e.path) != e.oid


class IndexLock:
    def __init__(self, path):
        self.path = path
        self.lock = path + '.lock'
        self.fd = None

    def acquire(self):
        """Take the lock, or exit 128 "File exists" as git does when it is held. Windows denies
        the create (PermissionError) while a delete of the last lock is still pending, the file
        still listed because another handle is open on it; real git reports that as a held lock
        (2.55.0.windows.5, probed 2026-10-14), and so does this. A denial with no file left (the
        delete completed in between) is tried again, as refs.RefLock.acquire does."""
        began = retry.start()
        while True:
            try:
                self.fd = os.open(self.lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o666)
                return self
            except FileExistsError:
                raise self._held()
            except PermissionError as e:
                if os.path.lexists(self.lock):
                    raise self._held()
                if not retry.pause(began):
                    raise retry.gave_up(e, began) from e

    def _held(self):
        return GitExit(128, "fatal: Unable to create '%s': File exists.\n\nAnother git process seems "
                            "to be running in this repository, e.g.\nan editor opened by 'git commit'. "
                            "Please make sure all processes\nare terminated then try again. If it still "
                            "fails, a git process\nmay have crashed in this repository earlier:\nremove "
                            "the file manually to continue.\n" % self.lock.replace('\\', '/'))

    def commit(self, data):
        try:
            os.write(self.fd, data)
        finally:
            os.close(self.fd)
            self.fd = None
        began = retry.start()
        while True:
            try:
                os.replace(self.lock, self.path)
                return
            except PermissionError as e:
                if not retry.pause(began):
                    raise retry.gave_up(e, began) from e

    def rollback(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            os.remove(self.lock)
        except OSError:
            pass
