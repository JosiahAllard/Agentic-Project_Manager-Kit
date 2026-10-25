"""The keys machine setup writes to the machine config (kitlib.config.user): the agents' temp sweep
key (kitlib.machinesetup.agenttemp) and the owner's email (kitlib.machinesetup.owner).

A survey reads the file once per run (snapshot(): one Snapshot per file, kept on the context;
with a PM-repo pointer the owner's email goes to the PM repository's local.json and the sweep key
to its config.json, kitlib.config.user.writable_file), and
every change that writes a key checks the file still holds what the run last saw. Each write moves
the snapshot on to the bytes it wrote, so two keys planned in one run both land, while a file
another program changed since the survey refuses the write. The file's other keys and its layout
(BOM, line ends, indent: kitlib.machinesetup.settings.read) are kept, and the file is put back when
the machine config cannot be read after the write. A folder with no .gitignore gets the one the
move writes (kitlib.config.move.GITIGNORE) before the first key lands, so local.json is never
unignored. Nothing here prints or exits.
"""
import os

from kitlib.config import ConfigError
from kitlib.config import move as kmove
from kitlib.config import user as kuser

from . import settings
from .common import is_link

NEW_LAYOUT = {'bom': False, 'crlf': False, 'final': True, 'indent': 2, 'ascii': False}


class Snapshot(object):
    """The machine config file `path` and the bytes this run last saw in it (`raw`, None: none)."""

    def __init__(self, path, raw):
        self.path, self.raw = path, raw


def snapshot(ctx, key):
    """(Snapshot, None) of the machine config file a write of `key` would change (the PM
    repository's config.json or local.json), read once per context; (None, why) when it cannot
    be written or read (kitlib.config.user.writable_file: no PM repo pointer, or a machine config
    still in the home folder, say)."""
    try:
        path = kuser.writable_file(ctx.home, key)
    except ConfigError as e:
        return None, str(e)
    kept = ctx.__dict__.setdefault('_machine_config_snapshots', {})
    if path in kept:
        return kept[path], None
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except FileNotFoundError:
        raw = None
    except OSError as e:
        return None, 'cannot read %s: %s' % (path, e)
    kept[path] = Snapshot(path, raw)
    return kept[path], None


def set_key(home, snap, key, value, replace=False):
    """Set `key` to `value` in the machine config file snap.path. RuntimeError, nothing written,
    when the file changed since snap.raw, is a link, or already has the key (unless `replace`)."""
    path = snap.path
    try:
        with open(path, 'rb') as f:
            now = f.read()
    except FileNotFoundError:
        now = None
    if now != snap.raw:
        raise RuntimeError('%s changed since the survey; run again' % path)
    if now is None:
        layout, data = NEW_LAYOUT, {}
    else:
        if is_link(path) or os.path.islink(path):
            raise RuntimeError('%s is a link; not replaced' % path)
        layout, data = settings.read(now)
        if not isinstance(data, dict):
            raise RuntimeError('%s holds no JSON object' % path)
    if key in data and not replace:
        raise RuntimeError('%s already has %s; not overwritten' % (path, key))
    data[key] = value
    new = settings.write(layout, data)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    if not os.path.lexists(os.path.join(folder, kmove.IGNORE_FILE)):
        kmove._write_new(os.path.join(folder, kmove.IGNORE_FILE), kmove.GITIGNORE.encode('utf-8'))
    _replace(path, new)
    try:
        kuser.user_config(home)
    except ConfigError as e:
        if now is None:
            os.remove(path)
        else:
            _replace(path, now)
        raise RuntimeError('the machine config could not be read after setting %s (%s); put back'
                           % (key, e))
    snap.raw = new


def _replace(path, data):
    tmp = path + '.machine-setup.tmp'
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
