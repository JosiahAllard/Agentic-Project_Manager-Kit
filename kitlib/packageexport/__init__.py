"""Host-neutral, tracked-subtree packages, verified before immutable publication.

This does not install or execute a host plugin. Callers supply reviewed full commit IDs,
relative subtrees and optional generated file replacements. No working-tree bytes are read.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from kitlib.git import batch, cmd


MARKER = '.kit-package.json'
SCHEMA = 1
_SHA = re.compile(r'^[0-9a-f]{40}(?:[0-9a-f]{24})?$')
_DEVICES = {'CON', 'PRN', 'AUX', 'NUL'} | {
    '%s%s' % (stem, number) for stem in ('COM', 'LPT') for number in '123456789¹²³'}


class PackageError(ValueError):
    """A package, path or reviewed identity could not be verified."""


def commit_id(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise PackageError('reviewed commit must be a full lowercase Git object ID')
    return value


def relative(value):
    """A portable file path without Windows aliases, traversal or metadata streams."""
    if not isinstance(value, str) or not value or any(c in value for c in '\\:*?"<>|\x00\r\n'):
        raise PackageError('unsafe package path: %r' % value)
    for part in value.split('/'):
        if (part in ('', '.', '..') or part.endswith(('.', ' '))
                or part.split('.')[0].rstrip(' ').upper() in _DEVICES
                or any(ord(c) < 32 for c in part)):
            raise PackageError('unsafe package path: %r' % value)
    return value


def no_links(path):
    """Refuse existing symlink/junction/reparse ancestors before following them."""
    path = Path(path)
    if not path.is_absolute():
        raise PackageError('package path must be absolute')
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise PackageError('package path contains a link: ' + str(candidate))
    return path.resolve()


def read_subtree(kit, sha, subtree):
    """Return relative paths and bytes from one reviewed Git commit; reject links/submodules."""
    commit_id(sha)
    relative(subtree)
    files = read_paths(kit, sha, (subtree,))
    if not files or subtree in files:
        raise PackageError('reviewed commit has no package subtree: ' + subtree)
    return {path[len(subtree) + 1:]: body for path, body in files.items()
            if path.startswith(subtree + '/')}


def read_paths(kit, sha, paths):
    """Read selected tracked files/directories from one commit, retaining their prefixes.

    Missing selections produce no files; callers enforce required runtime resources.
    This is a source reader, not an additional publication mechanism.
    """
    commit_id(sha)
    paths = tuple(relative(path) for path in paths)
    if not paths or len({path.casefold() for path in paths}) != len(paths):
        raise PackageError('package selections must be nonempty and distinct')
    found, why = batch.commits([sha], str(kit))
    if found is None or found.get(sha) != sha:
        raise PackageError('reviewed commit unavailable: ' + str(why or sha))
    raw, why = cmd.run(['ls-tree', '-r', '-z', sha, '--', *paths], str(kit))
    if raw is None:
        raise PackageError('cannot read package tree: ' + str(why))
    entries, seen = [], set()
    for row in raw.split(b'\0'):
        if not row:
            continue
        try:
            header, name = row.split(b'\t', 1)
            mode, kind, oid = header.decode('ascii').split()
            path = name.decode('utf-8')
        except (ValueError, UnicodeError) as error:
            raise PackageError('malformed package tree') from error
        if not any(path == chosen or path.startswith(chosen + '/') for chosen in paths):
            raise PackageError('package tree escaped the selected paths: ' + path)
        if mode not in ('100644', '100755') or kind != 'blob':
            raise PackageError('package tree contains a non-file or link: ' + path)
        tail = relative(path)
        if (tail.casefold() in seen or any(part.casefold() == MARKER.casefold()
                                         for part in tail.split('/'))):
            raise PackageError('package tree contains colliding or reserved paths: ' + tail)
        seen.add(tail.casefold())
        entries.append((tail, oid))
    blobs, why = batch.cat_file(sorted({oid for _, oid in entries}), str(kit))
    if blobs is None:
        raise PackageError('cannot read package files: ' + str(why))
    result = {}
    for path, oid in entries:
        value = blobs.get(oid)
        if value is None or value[0] != 'blob':
            raise PackageError('missing package blob: ' + path)
        result[path] = value[1]
    return result


def hashes(files):
    seen, result = set(), {}
    for name, body in sorted(files.items()):
        relative(name)
        if name.casefold() in seen or name.casefold() == MARKER.casefold() or not isinstance(body, bytes):
            raise PackageError('invalid package file: ' + name)
        seen.add(name.casefold())
        result[name] = hashlib.sha256(body).hexdigest()
    return result


def metadata(files, sha, subtree):
    return {'schema': SCHEMA, 'commit': commit_id(sha), 'subtree': relative(subtree),
            'files': hashes(files)}


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode('utf-8')


def verify(folder, expected=None):
    folder = no_links(folder)
    no_links(folder / MARKER)
    try:
        marker = json.loads((folder / MARKER).read_bytes())
    except (OSError, ValueError) as error:
        raise PackageError('package marker missing or invalid') from error
    if (not isinstance(marker, dict) or set(marker) != {'schema', 'commit', 'subtree', 'files'}
            or marker['schema'] != SCHEMA or not isinstance(marker['files'], dict)):
        raise PackageError('unsupported package marker')
    commit_id(marker['commit'])
    relative(marker['subtree'])
    actual = {}
    for base, directories, names in os.walk(folder, followlinks=False):
        for name in directories + names:
            no_links(Path(base) / name)
        for name in names:
            path = Path(base) / name
            tail = path.relative_to(folder).as_posix()
            if tail != MARKER:
                actual[relative(tail)] = path.read_bytes()
    if hashes(actual) != marker['files'] or (expected is not None and marker != expected):
        raise PackageError('package contents or reviewed identity changed')
    return marker


def publish(files, sha, subtree, destination):
    """Atomically publish complete bytes; keep a verified identical package, overwrite nothing."""
    destination = no_links(destination)
    expected = metadata(files, sha, subtree)
    if destination.exists():
        verify(destination, expected)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.kit-package-', dir=destination.parent))
    try:
        for name, body in files.items():
            target = stage.joinpath(*name.split('/'))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        (stage / MARKER).write_bytes(encoded(expected))
        verify(stage, expected)
        os.rename(stage, destination)
    finally:
        # Only this invocation's random staging folder, never an existing publication.
        if stage.exists():
            if stage.resolve().parent != destination.parent:
                raise PackageError('staging folder escaped the publication parent')
            shutil.rmtree(stage)
    return destination


@contextmanager
def exclusive(root):
    """One preparation writer. An existing lock is refused, never broken as stale."""
    root = no_links(root)
    path = root / '.kit-prepare.lock'
    try:
        handle = path.open('xb')
    except FileExistsError as error:
        raise PackageError('another preparation owns the store lock') from error
    try:
        handle.write(str(os.getpid()).encode('ascii'))
        handle.close()
        yield
    finally:
        handle.close()
        path.unlink()
