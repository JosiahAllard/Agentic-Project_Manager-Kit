"""Inactive, single-writer shared runtime versions; never modify host settings.

Expected identities come from reviewed preparation, not the marker being checked.
The mutable pointer binds both thin plugins to one immutable dependency directory.
"""
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from kitlib import packageexport as p
from kitlib import kitrelease as release


OWNER = '.kit-release-store.json'
STATE = 'state.json'
_OWNER = {'schema': 1, 'kind': 'shared-runtime-store', 'activation': 'disabled'}
_IDENTITY = re.compile(r'^[0-9a-f]{64}$')
_VERSION = re.compile(r'^[0-9a-f]{40}(?:[0-9a-f]{24})?-[0-9a-f]{64}$')
StoreError = p.PackageError


def _verify(root, identity, commit=None):
    try:
        return release.verify(root, identity, commit)
    except release.ReleaseError as error:
        raise StoreError(str(error)) from error


def _copy(root, identity, destination):
    try:
        return release.copy_release(root, identity, destination)
    except release.ReleaseError as error:
        raise StoreError(str(error)) from error


def _identity(value):
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise StoreError('expected release identity must be a full lowercase SHA256')
    return value


def _record(info):
    sha = p.commit_id(info['commit'])
    identity = _identity(info['identity'])
    return {'commit': sha, 'identity': identity, 'version': sha + '-' + identity}


def _checked_record(value):
    if not isinstance(value, dict) or set(value) != {'commit', 'identity', 'version'}:
        raise StoreError('invalid shared store release binding')
    expected = _record(value)
    if value != expected:
        raise StoreError('shared store version does not match its pinned release')
    return expected


def _read_json(path):
    p.no_links(path)
    try:
        raw = path.read_bytes()
        if len(raw) > 16384:
            raise StoreError('shared store metadata is too large')
        value = json.loads(raw)
    except (OSError, ValueError) as error:
        raise StoreError('shared store metadata is missing or invalid') from error
    if raw != p.encoded(value):
        raise StoreError('shared store metadata is not canonical')
    return value


def _load(root):
    root = p.no_links(root)
    if (not root.is_dir() or p.encoded(_read_json(root / OWNER)) != p.encoded(_OWNER)):
        raise StoreError('not an owned inactive shared release store')
    allowed = {OWNER, STATE, 'releases', '.kit-prepare.lock'}
    for child in root.iterdir():
        p.no_links(child)
        if child.name not in allowed:
            raise StoreError('unrecognized file in shared release store')
    versions = p.no_links(root / 'releases')
    if not versions.is_dir():
        raise StoreError('shared release versions directory missing')
    for child in versions.iterdir():
        p.no_links(child)
        if not child.is_dir() or not _VERSION.fullmatch(child.name):
            raise StoreError('unrecognized version in shared release store')
    state = _read_json(root / STATE)
    if (not isinstance(state, dict) or set(state) != {'schema', 'current', 'previous'}
            or type(state['schema']) is not int or state['schema'] != 1):
        raise StoreError('invalid shared store state')
    for key in ('current', 'previous'):
        if state[key] is not None:
            record = _checked_record(state[key])
            _verify(versions / record['version'], record['identity'], record['commit'])
    if state['current'] is None and state['previous'] is not None:
        raise StoreError('shared store predecessor without a current release')
    if state['current'] is not None and state['current'] == state['previous']:
        raise StoreError('shared store current and previous releases are identical')
    return root, state


def _create(root):
    root = p.no_links(root)
    if root.exists():
        _load(root)
        return root
    root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.kit-release-store-', dir=root.parent))
    try:
        (stage / OWNER).write_bytes(p.encoded(_OWNER))
        (stage / STATE).write_bytes(p.encoded({'schema': 1, 'current': None, 'previous': None}))
        (stage / 'releases').mkdir()
        try:
            os.rename(stage, root)
        except FileExistsError:
            # A competing creator is acceptable only if it published our complete layout.
            _load(root)
    finally:
        if stage.exists():
            if stage.resolve().parent != root.parent:
                raise StoreError('shared store staging directory escaped its parent')
            shutil.rmtree(stage)
    return root


def _cas(state, expected):
    if expected is not None:
        _identity(expected)
    actual = state['current']['identity'] if state['current'] is not None else None
    if actual != expected:
        raise StoreError('shared store current release changed; prepare again')


def _write_state(root, state):
    # All release bytes are verified before this sole atomic pointer publication.
    fd, name = tempfile.mkstemp(prefix='.kit-state-', dir=root)
    stage = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(p.encoded(state))
            handle.flush()
            os.fsync(handle.fileno())
        p.no_links(root / STATE)
        os.replace(stage, root / STATE)
    finally:
        if stage.exists():
            stage.unlink()


def _binding(root, state):
    record = state['current']
    return {'store': str(root), 'root': str(root / 'releases' / record['version']),
            'commit': record['commit'], 'identity': record['identity'],
            'previous': state['previous'], 'ready': False, 'activation': 'disabled'}


def install(store, artifact, expected_identity, expected_current=None):
    """Copy once, then select a pinned release with explicit expected-current CAS.

    None means the caller expects an empty store. No configuration, service, plugin
    cache or environment is changed. Existing releases are reused only after verification.
    """
    _identity(expected_identity)
    if expected_current is not None:
        _identity(expected_current)
    artifact = p.no_links(artifact)
    root = p.no_links(store)
    if artifact == root or artifact in root.parents or root in artifact.parents:
        raise StoreError('artifact and shared store must be separate directories')
    info = _verify(artifact, expected_identity)
    chosen = _record(info)
    if not root.exists() and expected_current is not None:
        raise StoreError('shared store has no expected current release')
    root = _create(root)
    with p.exclusive(root):
        root, state = _load(root)
        _cas(state, expected_current)
        destination = root / 'releases' / chosen['version']
        _copy(artifact, expected_identity, destination)
        _verify(destination, expected_identity, chosen['commit'])
        if chosen != state['current']:
            state = {'schema': 1, 'current': chosen, 'previous': state['current']}
            _write_state(root, state)
        return _binding(root, state)


def current(store, expected_identity):
    """Read and verify the selected dependency against the caller's reviewed pin."""
    _identity(expected_identity)
    root, state = _load(store)
    _cas(state, expected_identity)
    return _binding(root, state)


def rollback(store, expected_current, target_identity):
    """Select an explicitly pinned existing version; never infer/replay a predecessor."""
    _identity(expected_current)
    _identity(target_identity)
    if expected_current == target_identity:
        raise StoreError('rollback target must differ from current release')
    root, _ = _load(store)
    with p.exclusive(root):
        root, state = _load(root)
        _cas(state, expected_current)
        candidates = [path for path in (root / 'releases').iterdir()
                      if path.name.endswith('-' + target_identity)]
        if len(candidates) != 1:
            raise StoreError('rollback requires one existing pinned target release')
        info = _verify(candidates[0], target_identity)
        chosen = _record(info)
        if candidates[0].name != chosen['version']:
            raise StoreError('rollback directory does not match its release binding')
        state = {'schema': 1, 'current': chosen, 'previous': state['current']}
        _write_state(root, state)
        return _binding(root, state)
