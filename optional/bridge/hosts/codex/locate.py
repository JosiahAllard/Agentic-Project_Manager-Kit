"""The current Codex install for a runtime configuration written before a Codex update.

A Codex update moves both executables the configuration pins: the app package folder
(...\\WindowsApps\\OpenAI.Codex_<version>_<arch>__<publisher id>\\, holding app\\ChatGPT.exe) and the
CLI copy %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<16 hex>\\codex.exe. locate() maps each configured path
of those two shapes to its current counterpart; every other value is returned unchanged.

- A package path keeps its place inside the package and moves to the current install folder of
  the same package family. The family (name plus publisher id) comes from the configured path, so
  the publisher the owner reviewed stays pinned; Windows reports the folder
  (GetPackagesByPackageFamily, GetPackagePathByFullName).
- A bin path becomes the one bin\\<16 hex>\\codex.exe byte-identical to the current package's
  app\\resources\\codex.exe. The bin folder is the user's own, so the copy is bound to the
  package by its bytes, never by its folder name. No package path in the configuration means no
  reference: the bin path is left as configured, and the configuration check names it if it is gone.

The current install must sit directly in a WindowsApps folder, as every pin the owner reviewed
did, and the place inside the package must stay inside it (no '..', no drive or root): a package
registered from a folder the user can write to is refused (codex-package-location-refused).

Refusals are identifiers that name the setting, never a path (tools/bridge_service.py "_reason").
It runs at preparation and at every service start (win/runtime.parse_config); an update while the
service runs still needs a restart (STATUS.md).
"""
import ctypes
from ctypes import wintypes as w
import hashlib
from pathlib import Path
import re

from kitlib.proc.pipe import K, bind
from optional.bridge.core.messages import DeliveryError

PACKAGE_NAME = 'OpenAI.Codex'
PACKAGE_FOLDER = re.compile(r'OpenAI\.Codex_[0-9.]+_[A-Za-z0-9]+__([a-z0-9]{13})\Z', re.IGNORECASE)
BIN_FOLDER = re.compile(r'[0-9a-f]{16}\Z', re.IGNORECASE)
REFERENCE = ('app', 'resources', 'codex.exe')
CLI_NAME = 'codex.exe'
ERROR_INSUFFICIENT_BUFFER = 122
CHUNK = 1 << 20
CODE_MAX = 64
ROLE_CHARACTERS = frozenset('abcdefghijklmnopqrstuvwxyz0123456789-')

_by_family = bind(K, 'GetPackagesByPackageFamily',
    [w.LPCWSTR, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(w.LPWSTR),
     ctypes.POINTER(ctypes.c_uint32), w.LPWSTR], w.LONG)
_path_by_name = bind(K, 'GetPackagePathByFullName',
    [w.LPCWSTR, ctypes.POINTER(ctypes.c_uint32), w.LPWSTR], w.LONG)


def package_path(family):
    """The install folder of the one package of `family` installed for this user."""
    count, length = ctypes.c_uint32(0), ctypes.c_uint32(0)
    status = _by_family(family, ctypes.byref(count), None, ctypes.byref(length), None)
    if status not in (0, ERROR_INSUFFICIENT_BUFFER) or count.value != 1:
        raise DeliveryError('codex-package-unavailable')
    names, buffer = (w.LPWSTR * count.value)(), ctypes.create_unicode_buffer(length.value)
    if _by_family(family, ctypes.byref(count), names, ctypes.byref(length), buffer) != 0 or count.value != 1:
        raise DeliveryError('codex-package-unavailable')
    full_name = names[0]
    size = ctypes.c_uint32(0)
    if _path_by_name(full_name, ctypes.byref(size), None) not in (0, ERROR_INSUFFICIENT_BUFFER) or not size.value:
        raise DeliveryError('codex-package-unavailable')
    path = ctypes.create_unicode_buffer(size.value)
    if _path_by_name(full_name, ctypes.byref(size), path) != 0 or not path.value:
        raise DeliveryError('codex-package-unavailable')
    return Path(path.value)


def _package_place(value):
    """(family, path inside the package) for a configured package path, else None."""
    parts = Path(value).parts
    for index, part in enumerate(parts[1:], 1):
        match = PACKAGE_FOLDER.match(part)
        if match and parts[index-1].casefold() == 'windowsapps' and index+1 < len(parts):
            return PACKAGE_NAME + '_' + match.group(1).lower(), parts[index+1:]
    return None


def _role(name):
    """A setting name as a code part: lowercase, '_' as '-', any other character '-', capped so
    the longest code built on it fits _reason's [a-z][a-z0-9-]{0,63}."""
    role = ''.join(c if c in ROLE_CHARACTERS else '-' for c in name.lower().replace('_', '-'))
    return role[:CODE_MAX - len('codex-cli-image-ambiguous-')] or 'setting'


def _inside(place):
    """True when a place inside the package cannot leave it."""
    return bool(place) and all(part not in ('', '.', '..') and ':' not in part
        and '/' not in part and '\\' not in part for part in place)


def _bin_folder(value):
    """The bin folder of a configured bin\\<16 hex>\\codex.exe path, else None."""
    path = Path(value)
    parts = [part.casefold() for part in path.parts]
    if (len(parts) >= 6 and parts[-1] == CLI_NAME and BIN_FOLDER.match(path.parent.name)
            and parts[-5:-2] == ['openai', 'codex', 'bin']):
        return path.parent.parent
    return None


def _digest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(CHUNK), b''):
            digest.update(chunk)
    return digest.digest()


def _cli_copy(folder, reference, role):
    """The one bin\\<16 hex>\\codex.exe whose bytes are the package's reference copy."""
    try:
        size = reference.stat().st_size
        expected = _digest(reference)
        candidates = sorted(entry / CLI_NAME for entry in folder.iterdir()
            if BIN_FOLDER.match(entry.name) and (entry / CLI_NAME).is_file())
        matches = [path for path in candidates
            if path.stat().st_size == size and _digest(path) == expected]
    except OSError:
        raise DeliveryError('codex-cli-image-unmatched-' + role) from None
    if len(matches) > 1:
        raise DeliveryError('codex-cli-image-ambiguous-' + role)
    if not matches:
        raise DeliveryError('codex-cli-image-unmatched-' + role)
    return str(matches[0])


def locate(images, profile):
    """(images, profile) with each Codex package or bin path moved to the current install."""
    single = isinstance(images, str)
    listed = [images] if single else list(images) if isinstance(images, (list, tuple)) else None
    settings = dict(profile) if isinstance(profile, dict) else profile
    # (where the value sits, the setting named in a refusal, the value)
    values = [(('image', index), 'native-image-%d' % index, value) for index, value in enumerate(listed or ())]
    if isinstance(settings, dict):
        values += [(('profile', name), _role(name), value) for name, value in settings.items()
            if isinstance(name, str)]
    values = [entry for entry in values if isinstance(entry[2], str)]
    families = {place[0] for place in (_package_place(value) for _, _, value in values) if place}
    if not families:
        return images, profile
    if len(families) > 1:
        raise DeliveryError('codex-package-ambiguous')
    current = package_path(families.pop())
    if not current.is_absolute() or current.parent.name.casefold() != 'windowsapps':
        raise DeliveryError('codex-package-location-refused')
    moved = {}
    for where, role, value in values:
        place = _package_place(value)
        if place:
            if not _inside(place[1]):
                raise DeliveryError('codex-package-location-refused')
            moved[where] = str(current.joinpath(*place[1]))
            continue
        folder = _bin_folder(value)
        if folder is not None:
            moved[where] = _cli_copy(folder, current.joinpath(*REFERENCE), role)
    if isinstance(settings, dict):
        settings = {name: moved.get(('profile', name), value) for name, value in settings.items()}
    if listed is None:
        return images, settings
    listed = [moved.get(('image', index), value) for index, value in enumerate(listed)]
    return (listed[0] if single else listed), settings
