"""preflight's `config` line: which machine config files are read (kitlib.config.user.locate: the
PM repository's `.pm-kit` folder with a good PM-repo pointer), a machine config still in the home
folder (docs/kit-json.md "Moving the machine config into the PM repo"), and why the config cannot
be read. It names files and keys, never a value.

The `pointer` line (pointer_line) names the PM repository the PM-repo pointer gives
(kitlib.config.pointer), or why it gives none.
"""
import json
import os

from kitlib.config import ConfigError
from kitlib.config import pointer, user

PREFIX = 'config    '
POINTER_PREFIX = 'pointer   '


def _read(path):
    try:
        with open(path, encoding='utf-8-sig') as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        return e


def _fault(path, own, other, pm):
    """Why the strict reader refuses the PM repository's file `path` (holding the keys `own`; the
    keys `other` belong in the other file), in words and top-level key names only: its own message
    quotes the refused values (`not %r`), and this line never shows a value. None when it is absent
    or nothing in it alone is refused."""
    text = _read(path)
    if isinstance(text, FileNotFoundError):
        return None
    if isinstance(text, Exception):
        return 'cannot be read (%s)' % type(text).__name__
    seen = {'twice': False}

    def pairs(items):
        out = {}
        for k, v in items:
            seen['twice'] = seen['twice'] or k in out
            out[k] = v
        return out
    try:
        data = json.loads(text, object_pairs_hook=pairs)
    except (ValueError, RecursionError):
        return 'not valid JSON'
    if seen['twice']:
        return 'a key written twice in one object'
    if not isinstance(data, dict):
        return 'not a JSON object'
    placed = [k for k in data if k in other or k in user.DROPPED_KEYS]
    rest = {k: v for k, v in data.items() if k not in placed}
    if own is user.POLICY_KEYS:
        rest = user.resolved(rest, pm)
    keys = sorted(set(placed) | set(user.failing_keys(rest)))
    return 'bad key(s): %s' % ', '.join(keys) if keys else None


def _shown(path):
    return path.replace('\\', '/')


def line(home=None):
    """The one `config` line for the machine config under `home` (default the user's)."""
    loc = user.locate(home)
    if loc.error:
        return PREFIX + 'BROKEN %s' % loc.error
    if loc.state == user.NONE:
        return PREFIX + ('none - no PM-repo pointer and no machine config in the home folder: every '
                         'key has its default')
    names = [os.path.basename(f) for f in (loc.path, loc.local) if os.path.isfile(f)]
    try:
        user.user_config(home, location=loc)
    except ConfigError:
        faults = []
        for path, own, other in ((loc.path, user.POLICY_KEYS, user.LOCAL_KEYS),
                                 (loc.local, user.LOCAL_KEYS, user.POLICY_KEYS)):
            why = _fault(path, own, other, loc.pm)
            if why:
                faults.append('%s: %s' % (_shown(path), why))
        return PREFIX + 'BROKEN %s' % ('; '.join(faults) or '%s: refused by the strict reader'
                                       % _shown(loc.path))
    shown = '%s (%s)' % (_shown(loc.folder), ', '.join(names) if names else
                         'no file yet: every key has its default')
    if loc.leftover:
        return PREFIX + ('WARN %s is read; %s is still in the home folder and ignored - retire it '
                         'once the move is checked (%s)' % (shown, loc.display(loc.leftover),
                                                             user.MOVE_DOC))
    return PREFIX + shown


def pointer_line():
    """The one `pointer` line: the PM-repo pointer (kitlib.config.pointer) and the PM repository
    it names, none, or why it is BROKEN."""
    p = pointer.read()
    if p.state == pointer.OK:
        return POINTER_PREFIX + p.path.replace('\\', '/')
    if p.state == pointer.NONE:
        return POINTER_PREFIX + ('none - no PM-repo pointer (%s %s); an administrator sets it with '
                                 'machine_setup.py --pm-repo (docs/machine-setup.md "PM repo pointer")'
                                 % (pointer.KEY_NAME, pointer.VALUE))
    return POINTER_PREFIX + 'BROKEN %s %s: %s' % (pointer.KEY_NAME, pointer.VALUE, p.error)
