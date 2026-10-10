"""preflight's `config` line: which machine config file is read (kitlib.config.user.locate), and
a machine that has the file in both the new and the retired folder (docs/kit-json.md "Moving the
machine config folder"). It names files and keys, never a value.
"""
import json

from kitlib.config import ConfigError
from kitlib.config import migration, user

PREFIX = 'config    '


def _read(path):
    try:
        with open(path, encoding='utf-8-sig') as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        return e


def _fault(path):
    """Why the strict reader refuses the file at `path`, in words and top-level key names only:
    its own message quotes the refused values (`not %r`), and this line never shows a value."""
    text = _read(path)
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
    keys = user.failing_keys(data)
    return 'bad key(s): %s' % ', '.join(keys) if keys else 'refused by the strict reader'


def line(home=None):
    """The one `config` line for the machine config under `home` (default the user's)."""
    loc = user.locate(home)
    if loc.error:
        return PREFIX + 'BROKEN %s' % loc.error
    if loc.state == user.NONE:
        return PREFIX + 'none - no machine config; the kit reads and writes %s' % loc.display(loc.new)
    try:
        user.user_config(home, location=loc)
    except ConfigError:
        return PREFIX + 'BROKEN %s: %s' % (loc.display(), _fault(loc.path))
    if loc.state == user.OLD:
        return PREFIX + ('FALLBACK %s is read: %s is absent. Move it (docs/kit-json.md "Moving the '
                         'machine config folder")' % (loc.display(loc.old), loc.display(loc.new)))
    if loc.state == user.NEW:
        return PREFIX + loc.display(loc.new)
    new_text, old_text = _read(loc.new), _read(loc.old)
    if isinstance(old_text, Exception):
        how = 'the old one unreadable (%s)' % old_text
    elif new_text == old_text:
        how = 'same content'
    else:
        keys = migration.differing_keys(new_text, old_text)
        if keys == []:
            how = 'same values, laid out differently'
        else:
            how = 'DIFFERENT (%s)' % ('keys: ' + ', '.join(keys) if keys is not None
                                      else 'the old one is not a JSON object')
    return PREFIX + ('WARN both %s (read) and %s (ignored) exist, %s - retire the old folder '
                     '(docs/kit-json.md "Moving the machine config folder")'
                     % (loc.display(loc.new), loc.display(loc.old), how))
