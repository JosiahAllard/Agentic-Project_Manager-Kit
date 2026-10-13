"""The keys machine setup writes to the machine config (kitlib.config.user): the agents' temp sweep
key (kitlib.machinesetup.agenttemp) and the owner's email (kitlib.machinesetup.owner).

A survey reads the file once per run (snapshot(): one Snapshot per file, kept on the context), and
every change that writes a key checks the file still holds what the run last saw. Each write moves
the snapshot on to the bytes it wrote, so two keys planned in one run both land, while a file
another program changed since the survey refuses the write. The file's other keys and its layout
(BOM, line ends, indent: kitlib.machinesetup.settings.read) are kept, and the file is put back when
the machine config cannot be read after the write. Nothing here prints or exits.
"""
import os

from kitlib.config import ConfigError
from kitlib.config import user as kuser

from . import settings
from .common import is_link

NEW_LAYOUT = {'bom': False, 'crlf': False, 'final': True, 'indent': 2, 'ascii': False}


class Snapshot(object):
    """The machine config file `path` and the bytes this run last saw in it (`raw`, None: none)."""

    def __init__(self, path, raw):
        self.path, self.raw = path, raw


def snapshot(ctx):
    """(Snapshot, None) of the machine config file a write would change, read once per context;
    (None, why) when it cannot be written or read (kitlib.config.user.writable_file: a machine
    config only in the retired folder, say)."""
    try:
        path = kuser.writable_file(ctx.home)
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
    os.makedirs(os.path.dirname(path), exist_ok=True)
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
