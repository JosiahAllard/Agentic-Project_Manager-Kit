"""The object database of a real on-disk repository: loose objects (read and write) and packs
(read only), sha1 or sha256, plus parsing of commits, trees and tags."""
import hashlib
import os
import struct
import threading
import zlib

from . import retry
from .errors import Unsupported

TYPES = ('commit', 'tree', 'blob', 'tag')
_PACK_TYPES = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag'}
EMPTY_TREE = {'sha1': '4b825dc642cb6eb9a060e54bf8d69288fbee4904',
              'sha256': '6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321'}


def hash_object(algo, kind, data):
    h = hashlib.new(algo)
    h.update(b'%s %d\0' % (kind.encode('ascii'), len(data)))
    h.update(data)
    return h.hexdigest()


class Commit:
    __slots__ = ('oid', 'tree', 'parents', 'author', 'committer', 'message', 'raw_headers',
                 'encoding')

    def __init__(self, oid, data):
        self.oid = oid
        self.parents = []
        self.tree = None
        self.author = self.committer = b''
        self.encoding = None
        self.raw_headers = []
        head, sep, msg = data.partition(b'\n\n')
        if not sep:
            head, msg = data.rstrip(b'\n'), b''
        last = None
        for line in head.split(b'\n'):
            if line.startswith(b' ') and last is not None:
                self.raw_headers[-1] = (self.raw_headers[-1][0], self.raw_headers[-1][1] + b'\n' + line[1:])
                continue
            key, _, value = line.partition(b' ')
            last = key
            self.raw_headers.append((key, value))
            if key == b'tree':
                self.tree = value.decode('ascii')
            elif key == b'parent':
                self.parents.append(value.decode('ascii'))
            elif key == b'author':
                self.author = value
            elif key == b'committer':
                self.committer = value
            elif key == b'encoding':
                self.encoding = value.decode('ascii', 'replace')
        self.message = msg

    @property
    def commit_time(self):
        return ident_time(self.committer)[0]


def parse_ident(raw):
    """(name, email, timestamp int, tz str) from `Name <email> 123 +0000` bytes."""
    text = raw.decode('utf-8', 'replace')
    lt = text.find('<')
    gt = text.find('>', lt)
    name = text[:lt].strip() if lt >= 0 else ''
    email = text[lt + 1:gt] if lt >= 0 and gt >= 0 else ''
    rest = text[gt + 1:].split() if gt >= 0 else []
    ts = int(rest[0]) if rest and rest[0].lstrip('-').isdigit() else 0
    tz = rest[1] if len(rest) > 1 else '+0000'
    return name, email, ts, tz


def ident_time(raw):
    _, _, ts, tz = parse_ident(raw)
    return ts, tz


class Tag:
    __slots__ = ('oid', 'object', 'type', 'tag', 'tagger', 'message')

    def __init__(self, oid, data):
        self.oid = oid
        self.object = self.type = self.tag = None
        self.tagger = b''
        head, _, self.message = data.partition(b'\n\n')
        for line in head.split(b'\n'):
            key, _, value = line.partition(b' ')
            if key == b'object':
                self.object = value.decode('ascii')
            elif key == b'type':
                self.type = value.decode('ascii')
            elif key == b'tag':
                self.tag = value.decode('utf-8', 'replace')
            elif key == b'tagger':
                self.tagger = value


def parse_tree(data, hexlen):
    """[(mode str, name bytes, oid hex)] in stored order."""
    out = []
    raw = hexlen // 2
    i, n = 0, len(data)
    while i < n:
        sp = data.index(b' ', i)
        nul = data.index(b'\0', sp)
        mode = data[i:sp].decode('ascii')
        name = data[sp + 1:nul]
        oid = data[nul + 1:nul + 1 + raw].hex()
        out.append((mode, name, oid))
        i = nul + 1 + raw
    return out


def tree_sort_key(name, mode):
    """git's tree order: a tree sorts as if its name ended in '/'."""
    return name + b'/' if mode in ('40000', '040000') else name


def build_tree(entries):
    """Tree object bytes from [(mode str, name bytes, oid hex)] (sorted here)."""
    parts = []
    for mode, name, oid in sorted(entries, key=lambda e: tree_sort_key(e[1], e[0])):
        if mode == '040000':
            mode = '40000'
        parts.append(mode.encode('ascii') + b' ' + name + b'\0' + bytes.fromhex(oid))
    return b''.join(parts)


class Pack:
    """One .idx v2 + .pack pair, read only."""

    def __init__(self, idx_path, hexlen):
        self.idx_path = idx_path
        self.pack_path = idx_path[:-4] + '.pack'
        self.rawlen = hexlen // 2
        with open(idx_path, 'rb') as f:
            idx = f.read()
        if idx[:4] != b'\xfftOc' or struct.unpack('>I', idx[4:8])[0] != 2:
            raise Unsupported('pack index version in %s' % idx_path)
        fan = struct.unpack('>256I', idx[8:8 + 1024])
        self.count = fan[255]
        self.fan = fan
        pos = 8 + 1024
        self.names = idx[pos:pos + self.count * self.rawlen]
        pos += self.count * self.rawlen
        pos += self.count * 4                           # crc32
        self.offsets = idx[pos:pos + self.count * 4]
        pos += self.count * 4
        self.large = idx[pos:]
        self._fh = None
        self._lock = threading.Lock()

    def _name(self, i):
        return self.names[i * self.rawlen:(i + 1) * self.rawlen]

    def find(self, raw):
        first = raw[0]
        lo = self.fan[first - 1] if first else 0
        hi = self.fan[first]
        while lo < hi:
            mid = (lo + hi) // 2
            n = self._name(mid)
            if n < raw:
                lo = mid + 1
            elif n > raw:
                hi = mid
            else:
                return self._offset(mid)
        return None

    def prefixes(self, hexprefix):
        out = []
        for i in range(self.count):
            h = self._name(i).hex()
            if h.startswith(hexprefix):
                out.append(h)
        return out

    def all_names(self):
        return [self._name(i).hex() for i in range(self.count)]

    def _offset(self, i):
        off = struct.unpack('>I', self.offsets[i * 4:i * 4 + 4])[0]
        if off & 0x80000000:
            j = off & 0x7fffffff
            off = struct.unpack('>Q', self.large[j * 8:j * 8 + 8])[0]
        return off

    def read_at(self, offset, store):
        with self._lock:
            if self._fh is None:
                self._fh = open(self.pack_path, 'rb')
            return self._read(offset, store)

    def _read(self, offset, store):
        f = self._fh
        f.seek(offset)
        hdr = f.read(32)
        c = hdr[0]
        kind = (c >> 4) & 7
        size = c & 15
        shift = 4
        i = 1
        while c & 0x80:
            c = hdr[i]
            i += 1
            size |= (c & 0x7f) << shift
            shift += 7
        if kind == 6:                       # OFS_DELTA
            c = hdr[i]
            i += 1
            back = c & 0x7f
            while c & 0x80:
                c = hdr[i]
                i += 1
                back = ((back + 1) << 7) | (c & 0x7f)
            delta = self._inflate(offset + i)
            btype, bdata = self._read(offset - back, store)
            return btype, apply_delta(bdata, delta)
        if kind == 7:                       # REF_DELTA
            base = hdr[i:i + self.rawlen].hex()
            delta = self._inflate(offset + i + self.rawlen)
            got = store.read(base)
            if got is None:
                raise Unsupported('thin pack base %s missing' % base)
            f.seek(0)
            return got[0], apply_delta(got[1], delta)
        return _PACK_TYPES[kind], self._inflate(offset + i)

    def _inflate(self, pos):
        f = self._fh
        f.seek(pos)
        d = zlib.decompressobj()
        out = []
        while not d.eof:
            chunk = f.read(65536)
            if not chunk:
                break
            out.append(d.decompress(chunk))
        return b''.join(out)

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def apply_delta(base, delta):
    def varint(i):
        v = shift = 0
        while True:
            c = delta[i]
            i += 1
            v |= (c & 0x7f) << shift
            shift += 7
            if not c & 0x80:
                return v, i
    _src, i = varint(0)
    dst_size, i = varint(i)
    out = bytearray()
    n = len(delta)
    while i < n:
        c = delta[i]
        i += 1
        if c & 0x80:
            off = size = 0
            for bit in range(4):
                if c & (1 << bit):
                    off |= delta[i] << (8 * bit)
                    i += 1
            for bit in range(3):
                if c & (0x10 << bit):
                    size |= delta[i] << (8 * bit)
                    i += 1
            if size == 0:
                size = 0x10000
            out += base[off:off + size]
        elif c:
            out += delta[i:i + c]
            i += c
        else:
            raise Unsupported('delta opcode 0')
    if len(out) != dst_size:
        raise Unsupported('delta result size mismatch')
    return bytes(out)


_PACK_CACHE = {}
_PACK_LOCK = threading.Lock()


def _read_loose(path):
    """A loose object file's bytes, None when there is none. Windows denies an open
    (PermissionError) while another holder of the file excludes readers or the file is being
    replaced; that window is retried as refs._read_file retries a ref, and a denial that outlasts
    it is raised: an object that exists but cannot be read is never taken for a missing one."""
    began = retry.start()
    while True:
        try:
            with open(path, 'rb') as f:
                return f.read()
        except PermissionError as e:
            if os.path.isdir(path):
                return None
            if not retry.pause(began):
                raise retry.gave_up(e, began) from e
        except OSError:
            return None


class ObjectStore:
    def __init__(self, objdir, algo):
        self.objdir = objdir
        self.algo = algo
        self.hexlen = 40 if algo == 'sha1' else 64
        self._alternates = None
        self._packs = None
        self._cache = {}

    # -- packs --
    def _pack_list(self, refresh=False):
        if self._packs is None or refresh:
            packs = []
            pdir = os.path.join(self.objdir, 'pack')
            try:
                names = sorted(os.listdir(pdir))
            except OSError:
                names = []
            for n in names:
                if n.endswith('.idx') and os.path.exists(os.path.join(pdir, n[:-4] + '.pack')):
                    p = os.path.join(pdir, n)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    key = (os.path.normcase(p), st.st_size, st.st_mtime_ns)
                    with _PACK_LOCK:
                        pk = _PACK_CACHE.get(key)
                        if pk is None:
                            pk = _PACK_CACHE[key] = Pack(p, self.hexlen)
                    packs.append(pk)
            self._packs = packs
        return self._packs

    def alternates(self):
        if self._alternates is None:
            alts = []
            p = os.path.join(self.objdir, 'info', 'alternates')
            try:
                with open(p, encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            d = line if os.path.isabs(line) else os.path.join(self.objdir, line)
                            alts.append(ObjectStore(os.path.normpath(d), self.algo))
            except OSError:
                pass
            self._alternates = alts
        return self._alternates

    def _loose_path(self, oid):
        return os.path.join(self.objdir, oid[:2], oid[2:])

    def has(self, oid):
        if oid in self._cache:
            return True
        if os.path.exists(self._loose_path(oid)):
            return True
        raw = bytes.fromhex(oid)
        for refresh in (False, True):
            for p in self._pack_list(refresh):
                if p.find(raw) is not None:
                    return True
        return any(a.has(oid) for a in self.alternates())

    def read(self, oid):
        """(type, bytes) or None."""
        got = self._cache.get(oid)
        if got is not None:
            return got
        got = self._read(oid)
        if got is not None and len(self._cache) < 20000:
            self._cache[oid] = got
        return got

    def _read(self, oid):
        if len(oid) != self.hexlen:
            return None
        data = _read_loose(self._loose_path(oid))
        try:
            raw = zlib.decompress(data) if data is not None else None
        except zlib.error:
            raw = None
        if raw is not None:
            nul = raw.index(b'\0')
            kind, _, _size = raw[:nul].partition(b' ')
            return kind.decode('ascii'), raw[nul + 1:]
        rawid = bytes.fromhex(oid)
        for refresh in (False, True):
            for p in self._pack_list(refresh):
                off = p.find(rawid)
                if off is not None:
                    return p.read_at(off, self)
        for a in self.alternates():
            got = a.read(oid)
            if got is not None:
                return got
        return None

    def write(self, kind, data):
        oid = hash_object(self.algo, kind, data)
        if self.has(oid):
            return oid
        path = self._loose_path(oid)
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        payload = zlib.compress(b'%s %d\0' % (kind.encode('ascii'), len(data)) + data)
        tmp = os.path.join(d, 'tmp_obj_%d_%d' % (os.getpid(), threading.get_ident()))
        with open(tmp, 'wb') as f:
            f.write(payload)
        try:
            os.replace(tmp, path)
        except OSError:
            if not os.path.exists(path):
                raise
            try:
                os.remove(tmp)
            except OSError:
                pass
        try:
            os.chmod(path, 0o444)
        except OSError:
            pass
        self._cache[oid] = (kind, data)
        return oid

    def prefix_matches(self, prefix):
        """Every full oid starting with hex `prefix` (len >= 4)."""
        out = set()
        if len(prefix) >= 2:
            d = os.path.join(self.objdir, prefix[:2])
            try:
                for n in os.listdir(d):
                    full = prefix[:2] + n
                    if len(full) == self.hexlen and full.startswith(prefix):
                        out.add(full)
            except OSError:
                pass
        for p in self._pack_list(True):
            out.update(p.prefixes(prefix))
        for a in self.alternates():
            out.update(a.prefix_matches(prefix))
        return out

    def all_oids(self):
        out = set()
        try:
            dirs = os.listdir(self.objdir)
        except OSError:
            dirs = []
        for d in dirs:
            if len(d) == 2:
                try:
                    for n in os.listdir(os.path.join(self.objdir, d)):
                        if len(d + n) == self.hexlen:
                            out.add(d + n)
                except OSError:
                    pass
        for p in self._pack_list(True):
            out.update(p.all_names())
        return out
