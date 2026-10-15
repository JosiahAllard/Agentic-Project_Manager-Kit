"""Which kit copies on this machine would refuse a machine config key, so the owner refreshes
the plugin before setting a key a newer kit adds.

Every kit copy reads the one machine config, and kitlib.config.user refuses a whole file that
holds a top-level key its own SCHEMA does not know. So a key a newer kit adds (`hub`, say) is safe
to set only once every copy that still runs knows it. This module finds the copies and reads each
one's kitlib/config/user.py as text: it parses the module-level SCHEMA dict and RETIRED tuple with
`ast` and never imports or runs the file. It writes nothing.

  find(root, current, installs)     the copies: every export folder <root>/<sha12>, every pin
                                    <root>/pins/<sha12> (kitlib.gate.pin) and each account's
                                    install (`installs`, from kitlib.pluginsource.installs), once
                                    each
  check(found, keys, refresh)       one line per copy, and whether every copy in use knows `keys`

A copy is in use when it is the export the marketplace names (`current`), a pin, or an account
folder's install. Any other export folder is not in use, though a session started from it before
the last refresh may still run it; those decide nothing and are counted apart (Report.stale).

Only top-level keys are checked: a copy that knows a key may still refuse an inner key of it,
which only that copy's owning module checks when it reads it.
"""
import ast
import collections
import os
import re

from .types import ConfigError, is_note, parse_json

USER_PY = ('kitlib', 'config', 'user.py')
PINS = 'pins'                                   # kitlib.gate.pin.PINS
_SHA12 = re.compile(r'^[0-9a-f]{12}$')

CURRENT = 'current export'
EXPORT = 'not in use'
PIN = 'pin'
INSTALL = 'installed in %s'

KNOWS = '%s (%s): knows every key'
REFUSES = '%s (%s): would refuse %s'
UNKNOWN = '%s (%s): cannot tell: %s'
MISSING = 'the folder is missing: refresh that account (%s)'
NONE_FOUND = 'no kit copy found: no export folder, pin or account install'


class CopyError(Exception):
    """A copy's user.py cannot be read as a SCHEMA of literal keys; str() says why."""


Copy = collections.namedtuple('Copy', 'path role in_use')
Report = collections.namedtuple('Report', 'ready lines stale')


def _strings(node):
    """The literal strings of a tuple or list node; CopyError for anything else in it."""
    if not isinstance(node, (ast.Tuple, ast.List)):
        raise CopyError('RETIRED is not a literal tuple')
    out = []
    for elt in node.elts:
        if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
            raise CopyError('RETIRED holds a name that is not a literal string')
        out.append(elt.value)
    return frozenset(out)


def schema_keys(folder):
    """(the keys `folder`'s SCHEMA names, the keys its RETIRED tuple names), both frozensets; a
    copy with no RETIRED retires nothing. CopyError when the file cannot be read or parsed, has
    no module-level SCHEMA dict literal, or a SCHEMA key is not a literal string."""
    path = os.path.join(folder, *USER_PY)
    try:
        with open(path, encoding='utf-8-sig') as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        raise CopyError('cannot read %s: %s' % (path, e.__class__.__name__))
    try:
        tree = ast.parse(text, path)
    except (SyntaxError, ValueError):
        raise CopyError('cannot parse %s' % path)
    known = retired = None
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        name = node.targets[0].id
        if name == 'SCHEMA' and isinstance(node.value, ast.Dict):
            keys = []
            for k in node.value.keys:
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    raise CopyError('a SCHEMA key in %s is not a literal string' % path)
                keys.append(k.value)
            known = frozenset(keys)
        elif name == 'RETIRED':
            retired = _strings(node.value)
    if known is None:
        raise CopyError('no SCHEMA dict in %s' % path)
    return known, retired or frozenset()


def refused(folder, keys):
    """The sorted keys of `keys` that `folder`'s copy refuses: neither a note, known nor retired.
    CopyError as schema_keys."""
    known, retired = schema_keys(folder)
    return tuple(sorted(k for k in set(keys) if not is_note(k) and k not in known
                        and k not in retired))


def config_keys(path, wanted=()):
    """The sorted top-level keys of the machine config file `path` besides its notes, plus
    `wanted` (the keys about to be set). An absent file holds `wanted` alone. ConfigError for a
    file that cannot be read or is not a JSON object."""
    try:
        with open(path, encoding='utf-8-sig') as fh:
            text = fh.read()
    except FileNotFoundError:
        return tuple(sorted(set(wanted)))
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError('cannot read %s: %s' % (path, e))
    data = parse_json(text, path)
    if not isinstance(data, dict):
        raise ConfigError('%s is not a JSON object' % path)
    return tuple(sorted({k for k in data if not is_note(k)} | set(wanted)))


def _dirs(top):
    try:
        names = sorted(os.listdir(top))
    except OSError:
        return []
    return [n for n in names if _SHA12.match(n) and os.path.isdir(os.path.join(top, n))]


def find(root, current=None, installs=()):
    """The kit copies (Copy: path, role, in_use) under the export root `root` and in `installs`,
    each folder once with its roles joined by ', '. `current`: the export folder name the
    marketplace names (kitlib.pluginsource.current_folder), or None. `installs`: [(install path,
    account folder)], each an account's install (role INSTALL), whether or not the path exists."""
    roles = collections.OrderedDict()           # normcase path -> [path, [roles], in_use]

    def add(path, role, in_use):
        key = os.path.normcase(os.path.normpath(path))
        rec = roles.setdefault(key, [os.path.normpath(path), [], False])
        rec[1].append(role)
        rec[2] = rec[2] or in_use

    for name in _dirs(root):
        is_current = current is not None and name == current
        add(os.path.join(root, name), CURRENT if is_current else EXPORT, is_current)
    for name in _dirs(os.path.join(root, PINS)):
        add(os.path.join(root, PINS, name), PIN, True)
    for path, account in installs:
        add(path, INSTALL % account, True)
    out = []
    for path, names, in_use in roles.values():
        if in_use and EXPORT in names:
            names = [n for n in names if n != EXPORT]
        out.append(Copy(path, ', '.join(names), in_use))
    return out


def check(found, keys, refresh='the plugin refresh'):
    """Report(ready, lines, stale) for the copies `found` and the machine config keys `keys`:
    one line per copy (KNOWS, REFUSES or UNKNOWN; a folder that is not there says MISSING, naming
    `refresh`, the refresh command); ready is True when there is a copy in use and every copy in
    use knows every key; stale counts the copies not in use that refuse a key or cannot be read."""
    if not found:
        return Report(False, [NONE_FOUND], 0)
    lines, ready, stale = [], True, 0
    for c in found:
        try:
            if not os.path.isdir(c.path):
                raise CopyError(MISSING % refresh)
            bad = refused(c.path, keys)
        except CopyError as e:
            lines.append(UNKNOWN % (c.path, c.role, e))
            bad = None
        else:
            lines.append(REFUSES % (c.path, c.role, ', '.join(bad)) if bad
                         else KNOWS % (c.path, c.role))
        if bad is None or bad:
            if c.in_use:
                ready = False
            else:
                stale += 1
    return Report(ready and any(c.in_use for c in found), lines, stale)
