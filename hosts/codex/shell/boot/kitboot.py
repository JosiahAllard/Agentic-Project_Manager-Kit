"""kitboot: the check every host entry runs before it imports kitlib (ARCHITECTURE.md "Packaging";
docs/multi-host-plan.md section 2.5; docs/kitboot.md). Standard library only, and nothing from the
kit is imported here: this file decides which kit may be imported. Lifted from the Codex plugin's
profile.py (discover, load_kit) and release_binding.py (verify).

  boot(environ=None, here=None) -> str
      locate() then bind(): the kit root, first on sys.path. What an entry calls.
  locate(environ=None, here=None) -> Kit(root, identity, commit)
      which kit to run, checked, without touching sys.path:
      - PM_KIT_ROOT unset: the source checkout this file is in (the folder above boot/).
        PM_KIT_RELEASE_ID set without PM_KIT_ROOT is refused.
      - PM_KIT_ROOT set: a non-blank absolute path.
      - PM_KIT_RELEASE_ID set: the release at the root is verified against that pin before any
        kitlib import (verify()); the Kit carries the pin and the release's commit.
      - a release (it holds MARKER) with no pin is refused, so an unpinned release never runs.
      - the root must hold kitlib/__init__.py and tools/machine_lock.py.
  bind(kit) -> str
      put kit.root first on sys.path (pinned: no bytecode written) and refuse a kitlib already
      loaded from anywhere else; returns the root.
  verify(root, identity, commit=None) -> dict
      the release's own verifier (VERIFIER), read once, hashed against the pinned marker's entry
      for it and run from those bytes; it then checks every file of the release.

`here` stands in for this file (tests). Every refusal is BootError; an entry fails closed on it
(the guard denies, a session hook says it could not load the kit, the lock server exits).
"""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT_ENV = 'PM_KIT_ROOT'
RELEASE_ENV = 'PM_KIT_RELEASE_ID'
MARKER = '.kit-package.json'
SUBTREE = 'shared-runtime'
VERIFIER = 'kitlib/kitrelease/bootstrap.py'
REQUIRED = ('kitlib/__init__.py', 'tools/machine_lock.py')
MAX_MARKER = 8 * 1024 * 1024
MAX_VERIFIER = 1024 * 1024
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_SHA = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?\Z')

Kit = collections.namedtuple('Kit', 'root identity commit')


class BootError(RuntimeError):
    """The kit cannot be run: a bad PM_KIT_ROOT or pin, a release that fails its check, or a
    different kitlib already loaded."""


def _no_links(path):
    """`path` (absolute) when neither it nor a parent is a link or a junction."""
    path = Path(path)
    if not path.is_absolute():
        raise BootError('kit release path must be absolute: %s' % path)
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise BootError('kit release path contains a link: %s' % item)
    return path


def _read(path, limit, what):
    with _no_links(path).open('rb') as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise BootError('%s exceeds its size bound' % what)
    return data


def verify(root, identity, commit=None):
    """The release verifier's result for `root`; BootError when it is not the pinned release."""
    if not isinstance(identity, str) or not _DIGEST.match(identity):
        raise BootError('%s must be a pinned lowercase SHA-256 identity' % RELEASE_ENV)
    if commit is not None and (not isinstance(commit, str) or not _SHA.match(commit)):
        raise BootError('the release commit must be a full lowercase Git object ID')
    root = _no_links(root)
    try:
        marker = json.loads(_read(root / MARKER, MAX_MARKER, 'the release marker'))
        canonical = (json.dumps(marker, sort_keys=True, indent=2) + '\n').encode('utf-8')
    except (OSError, ValueError, UnicodeError, RecursionError) as e:
        raise BootError('the release marker is missing or invalid: %s' % e) from None
    if hashlib.sha256(canonical).hexdigest() != identity:
        raise BootError('the release marker differs from the pinned %s' % RELEASE_ENV)
    if (not isinstance(marker, dict) or set(marker) != {'schema', 'commit', 'subtree', 'files'}
            or marker['schema'] != 1 or marker['subtree'] != SUBTREE
            or not isinstance(marker['files'], dict)):
        raise BootError('unsupported release marker')
    if commit is not None and marker['commit'] != commit:
        raise BootError('the release commit differs from the reviewed commit')
    try:
        body = _read(root / VERIFIER, MAX_VERIFIER, 'the release verifier')
    except OSError as e:
        raise BootError('the release verifier cannot be read: %s' % e) from None
    if hashlib.sha256(body).hexdigest() != marker['files'].get(VERIFIER):
        raise BootError('the release verifier differs from the pinned release')
    space = {'__file__': str(root / VERIFIER), '__name__': '_kitboot_release_verifier'}
    try:
        exec(compile(body, str(root / VERIFIER), 'exec'), space)
        return space['verify'](root, identity, commit)
    except BootError:
        raise
    except Exception as e:                      # the verifier's ReleaseError, or its own fault
        raise BootError('the release failed its check: %s' % e) from None


def _source(here):
    return Path(os.path.abspath(here or __file__)).parent.parent


def locate(environ=None, here=None):
    env = os.environ if environ is None else environ
    configured, pin = env.get(ROOT_ENV), env.get(RELEASE_ENV)
    if pin is not None and configured is None:
        raise BootError('a pinned release (%s) needs %s' % (RELEASE_ENV, ROOT_ENV))
    if configured is not None:
        if not configured.strip():
            raise BootError('%s must be a non-blank absolute path' % ROOT_ENV)
        root = Path(configured)
        if not root.is_absolute():
            raise BootError('%s must be absolute: %s' % (ROOT_ENV, configured))
        if pin is None:
            root = root.resolve()
    else:
        root = _source(here)
    commit = None
    if pin is not None:
        result = verify(root, pin)
        root, commit = Path(result['root']), result['commit']
    elif (root / MARKER).exists():
        raise BootError('%s is a kit release; it runs only with %s pinned' % (root, RELEASE_ENV))
    for rel in REQUIRED:
        if not (root / rel).is_file():
            raise BootError('no kit at %s (no %s); set %s to the kit folder' % (root, rel, ROOT_ENV))
    return Kit(str(root), pin, commit)


def bind(kit):
    root = os.path.abspath(kit.root)
    want = os.path.normcase(os.path.realpath(os.path.join(root, 'kitlib', '__init__.py')))
    loaded = sys.modules.get('kitlib')
    if loaded is not None:
        origin = getattr(loaded, '__file__', None)
        if origin is None or os.path.normcase(os.path.realpath(origin)) != want:
            raise BootError('another kitlib is already loaded (%s), not %s' % (origin, root))
    if kit.identity is not None:
        sys.dont_write_bytecode = True
    if not sys.path or os.path.normcase(os.path.abspath(sys.path[0])) != os.path.normcase(root):
        sys.path.insert(0, root)
    return root


def boot(environ=None, here=None):
    return bind(locate(environ, here))
