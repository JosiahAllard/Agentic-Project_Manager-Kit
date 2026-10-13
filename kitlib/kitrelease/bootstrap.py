"""Standalone release verifier, safe to execute after its own hash is pinned.

Only the standard library is imported. The expected identity must come from trusted
reviewed preparation, never from the artifact being checked. No Git or activation.
The host shell folders shipped (ROOTS) come from the hosts.json beside it (host_roots), so no
host is named here; that file is itself in every release's checked inventory.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat


MARKER = '.kit-package.json'
SUBTREE = 'shared-runtime'
HOSTS = 'hosts.json'
CORE_ROOTS = ('kitlib', 'tools', 'mcp', 'docs', 'templates', 'githooks', 'installer', 'optional',
              'knowledge')
RESOURCES = ('AGENTS.md', 'ARCHITECTURE.md', 'CLAUDE.md', 'README.md',
             'LICENSE', 'reserve_ui.pyw', HOSTS)
REQUIRED_FILE = 'kitlib/kitrelease/bootstrap.py'
_SHA = re.compile(r'^[0-9a-f]{40}(?:[0-9a-f]{24})?$')
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_DEVICES = {'CON', 'PRN', 'AUX', 'NUL'} | {
    '%s%s' % (stem, number) for stem in ('COM', 'LPT') for number in '123456789¹²³'}


class ReleaseError(ValueError):
    """Untrusted or changed runtime release, identity or path."""


def host_roots(path=None):
    """The top folder of each registered host's shell, in hosts.json order: the release ships every
    host the registry names and no other. Read with json alone, not kitlib.host.registry, because
    this file runs before kitlib is trusted. `path` defaults to the hosts.json beside this kit; in a
    release that file is in the verified inventory (RESOURCES), so a changed one fails verify."""
    path = Path(__file__).resolve().parents[2] / HOSTS if path is None else Path(path)
    try:
        hosts = json.loads(path.read_bytes(), object_pairs_hook=_pairs)
    except (OSError, ValueError) as error:
        raise ReleaseError('host registry missing or invalid') from error
    if not isinstance(hosts, dict) or not hosts:
        raise ReleaseError('host registry missing or invalid')
    roots = []
    for entry in hosts.values():
        shell = entry.get('shell') if isinstance(entry, dict) else None
        root = _relative(shell if isinstance(shell, str) else '').split('/')[0]
        if root not in roots and root not in CORE_ROOTS:
            roots.append(root)
    return tuple(roots)


def _relative(value):
    if (not isinstance(value, str) or not value
            or any(c in value for c in '\\:*?"<>|\x00\r\n')):
        raise ReleaseError('unsafe release path')
    for part in value.split('/'):
        if (part in ('', '.', '..') or part.endswith(('.', ' '))
                or part.split('.')[0].rstrip(' ').upper() in _DEVICES
                or any(ord(c) < 32 for c in part)):
            raise ReleaseError('unsafe release path')
    return value


def _no_links(path):
    path = Path(path)
    if not path.is_absolute():
        raise ReleaseError('release path must be absolute')
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ReleaseError('release path contains a link')
    return path.resolve()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ReleaseError('duplicate release metadata field')
        result[key] = value
    return result


ROOTS = CORE_ROOTS + host_roots()


def _allowed(name):
    _relative(name)
    parts = name.split('/')
    return (name in RESOURCES or (len(parts) > 1 and parts[0] in ROOTS
                                  and not _excluded(parts)
                                  and not name.casefold().endswith(('.pyc', '.pyo'))))


def _excluded(parts):
    denied = {'.git', 'tests', '__pycache__', MARKER.casefold()}
    if parts[0] != 'templates':
        denied.update({'.claude', '.codex'})
    return any(part.casefold() in denied for part in parts[1:])


def _metadata(root, expected_identity, expected_commit):
    if not isinstance(expected_identity, str) or not _DIGEST.fullmatch(expected_identity):
        raise ReleaseError('trusted expected identity must be a lowercase SHA-256 digest')
    if expected_commit is not None and (not isinstance(expected_commit, str)
                                       or not _SHA.fullmatch(expected_commit)):
        raise ReleaseError('expected commit must be a full lowercase Git object ID')
    marker_path = _no_links(root / MARKER)
    try:
        if not stat.S_ISREG(marker_path.stat().st_mode) or marker_path.stat().st_size > 16 * 1024 * 1024:
            raise ReleaseError('invalid release metadata file')
        marker = json.loads(marker_path.read_bytes(), object_pairs_hook=_pairs)
    except (OSError, ValueError) as error:
        raise ReleaseError('release metadata missing or invalid') from error
    if (not isinstance(marker, dict) or set(marker) != {'schema', 'commit', 'subtree', 'files'}
            or type(marker['schema']) is not int or marker['schema'] != 1
            or marker['subtree'] != SUBTREE or not isinstance(marker['commit'], str)
            or not _SHA.fullmatch(marker['commit']) or not isinstance(marker['files'], dict)):
        raise ReleaseError('unsupported release metadata')
    identity = hashlib.sha256((json.dumps(marker, sort_keys=True, indent=2) + '\n').encode('utf-8')).hexdigest()
    if identity != expected_identity or (expected_commit is not None and marker['commit'] != expected_commit):
        raise ReleaseError('release differs from trusted reviewed identity')
    seen = set()
    for name, digest in marker['files'].items():
        if (not _allowed(name) or name.casefold() in seen or not isinstance(digest, str)
                or not _DIGEST.fullmatch(digest)):
            raise ReleaseError('invalid release file inventory')
        seen.add(name.casefold())
    if (REQUIRED_FILE not in marker['files'] or not any(name.startswith('tools/') for name in marker['files'])):
        raise ReleaseError('release lacks mandatory runtime resources')
    return marker


def verify(root, expected_identity, expected_commit=None):
    """Check the entire artifact against an independently trusted expected identity."""
    root = _no_links(root)
    marker = _metadata(root, expected_identity, expected_commit)
    actual, seen = {}, set()

    def walk_error(error):
        raise ReleaseError('cannot enumerate release') from error

    try:
        for base, directories, names in os.walk(root, followlinks=False, onerror=walk_error):
            for name in directories + names:
                path = _no_links(Path(base) / name)
                tail = _relative(path.relative_to(root).as_posix())
                if tail.casefold() in seen:
                    raise ReleaseError('release paths collide')
                seen.add(tail.casefold())
                if name in directories:
                    if tail.split('/')[0] not in ROOTS or _excluded(tail.split('/')):
                        raise ReleaseError('unexpected release directory')
                    continue
                if tail == MARKER:
                    continue
                if not _allowed(tail) or not stat.S_ISREG(path.stat().st_mode):
                    raise ReleaseError('unexpected release file')
                digest = hashlib.sha256()
                with path.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(chunk)
                actual[tail] = digest.hexdigest()
    except OSError as error:
        raise ReleaseError('cannot read release content') from error
    if actual != marker['files']:
        raise ReleaseError('release inventory or content changed')
    # Re-read the metadata after inventory enumeration, refusing changed bindings.
    if _metadata(root, expected_identity, expected_commit) != marker:
        raise ReleaseError('release metadata changed during verification')
    return {'root': str(root), 'commit': marker['commit'], 'identity': expected_identity,
            'ready': False}
