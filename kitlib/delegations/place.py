"""Where a hub-approved run may act (docs/delegations.md "Hub approvals").

check(values, tokens, cfg, kit_root, ...) -> the whys, empty when every path the run names may be
acted on. It judges each filled argument value that holds `/`, `\\` or `:`, and each token of the
filled command after the program: a token of an option's shape (`/MIR`, `-o`, `/LOG:<path>`,
`--out=<path>`) only by its value part, and only when that holds `/`, `\\` or `:`; any other
token holding one of them as a whole. A text that is `.` or `..`, or ends in `=.` or `=..`
(`dest=..`), is judged too. Each path judged must:

  - be absolute with a drive (`<drive>:\\`): a relative, drive-relative, UNC or `\\\\?\\` path
    is refused;
  - lie under the Dev Drive, the machine config's `dev_drive` (kitlib.config.user.dev_drive),
    both as given and with its nearest existing folder resolved, and not be its root. With no
    `dev_drive` set every path is refused;
  - have no folder above it, as given or resolved, that holds `.git` (inside a checkout);
  - hold no checkout: a scan below it, of at most `scan_cap` entries, finds no `.git` and no link
    (a link could lead to one). Past the cap it is refused: the scan fails closed;
  - be none of the owner-only places (owner_only), lie in none and hold none.

It reads only the file system; it runs nothing and writes nothing. It judges the paths named on
the command line, not where a program writes by default (docs/delegations.md "Known limits").
"""
import os
import re

from kitlib.config import user as kuser
from kitlib.host import registry
from kitlib.pluginsource import RefreshError
from kitlib.pluginsource import settings as plugin_settings

SCAN_CAP = 20000
# An option token: -x, --name, /NAME, each optionally with :value or =value.
OPTION = re.compile(r'(?:--?|/)[A-Za-z][A-Za-z0-9_-]*(?:[:=](.*))?\Z', re.S)
DRIVE_PATH = re.compile(r'[A-Za-z]:[\\/]')
# A path character, or a bare `.` or `..` (whole, or after `=` as in `dest=..`): a relative path.
PATHY = re.compile(r'[\\/:]|(?:\A|=)\.\.?\Z')

WHY_NO_DEV = 'the machine config sets no dev_drive (the Dev Drive\'s root folder): no path may be acted on'
WHY_RELATIVE = '%s: not an absolute path with a drive; give it from the Dev Drive (%s)'
WHY_OFF = '%s: not on the Dev Drive (%s)'
WHY_ROOT = '%s: the Dev Drive root'
WHY_INSIDE = '%s: inside the checkout %s'
WHY_HOLDS = '%s: holds the checkout %s'
WHY_LINK = '%s: holds the link %s, which could lead anywhere; refused'
WHY_CAP = '%s: over %d entries below it to check; refused'
WHY_OWNER = '%s: owner-only (%s %s)'
WHY_HOLDS_OWNER = '%s: owner-only (it holds %s %s)'
WHY_UNREAD = '%s: cannot be checked: %s'


def _key(path):
    return os.path.normcase(os.path.normpath(path))


def _under(path, top):
    """True when `path` is `top` or inside it (both already _key'd)."""
    return path == top or path.startswith(top.rstrip(os.sep) + os.sep)


def _strip(tok):
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in '"\'':
        return tok[1:-1]
    return tok


def candidates(values, tokens):
    """The texts judged as paths, in order, each once: the argument values holding `/`, `\\` or
    `:`, then the paths the command tokens name (the module docstring)."""
    out = []
    for name in sorted(values):
        v = _strip(values[name])
        if PATHY.search(v):
            out.append(v)
    for tok in tokens:
        tok = _strip(tok)
        m = OPTION.match(tok)
        if m:
            tok = _strip(m.group(1) or '')
        if PATHY.search(tok):
            out.append(tok)
    return list(dict.fromkeys(out))


def owner_only(cfg, home, kit_root, environ=None):
    """([(label, path)], whys): the places no approved run may touch. The machine config folders
    (both names kitlib.config.user.locate reads: the config, the hub secrets, the delegation
    list), every host's state folder (the machine lock), the hub store and the PM data worktree,
    the ledger, the kit release store and the running kit. A place that cannot be named is a why:
    the caller refuses then."""
    loc = kuser.locate(home)
    places = [('the machine config folder', os.path.dirname(loc.new)),
              ('the machine config folder', os.path.dirname(loc.old))]
    places += [('a host state folder', os.path.join(home, d))
               for d in registry.union('state_directory')]
    hub = cfg.get('hub') or {}
    if isinstance(hub.get('store'), str) and os.path.isabs(hub['store']):
        places.append(('the hub store', hub['store']))
    pm = cfg.get('pm_data') or {}
    if isinstance(pm.get('path'), str) and os.path.isabs(pm['path']):
        places.append(('the PM data worktree', pm['path']))
    for key, label in (('ledger_dir', 'the ledger folder'), ('ledger_path', 'the ledger')):
        if isinstance(cfg.get(key), str) and os.path.isabs(cfg[key]):
            places.append((label, cfg[key]))
    whys = []
    try:
        places.append(('the kit release store',
                       plugin_settings(cfg.get('plugin'), environ).export_dir))
    except RefreshError as e:
        whys.append('the kit release store cannot be placed (%s): every path is refused' % e)
    places.append(('the running kit', kit_root))
    return places, whys


def _resolved(path):
    """`path` with its nearest existing folder's links resolved (os.path.realpath)."""
    head, tail = path, []
    while not os.path.lexists(head):
        parent = os.path.dirname(head)
        if parent == head:
            break
        tail.append(os.path.basename(head))
        head = parent
    return os.path.join(os.path.realpath(head), *reversed(tail))


def _place_keys(place):
    """An owner-only place as given and with its links resolved, each _key'd."""
    keys = [_key(place)]
    try:
        keys.append(_key(os.path.realpath(place)))
    except (OSError, ValueError):
        pass
    return list(dict.fromkeys(keys))


def _checkout_above(path):
    """The nearest folder at or above `path` holding `.git`, or None."""
    p = path
    while True:
        if os.path.lexists(os.path.join(p, '.git')):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def _scan(path, cap):
    """None when nothing below `path` (a folder) is a checkout or a link, else (why form, what)."""
    stack, seen = [path], 0
    while stack:
        top = stack.pop()
        with os.scandir(top) as it:
            for entry in it:
                seen += 1
                if seen > cap:
                    return WHY_CAP, cap
                if os.path.normcase(entry.name) == '.git':
                    return WHY_HOLDS, top
                if entry.is_symlink() or entry.is_junction():
                    return WHY_LINK, entry.path
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
    return None


def _judge(text, dev, places, cap):
    if text.startswith(('\\\\', '//')) or not DRIVE_PATH.match(text):
        return [WHY_RELATIVE % (text, dev)]
    given = os.path.normpath(text)
    try:
        real = _resolved(given)
    except (OSError, ValueError) as e:
        return [WHY_UNREAD % (text, e)]
    devs = {_key(dev)}
    try:
        devs.add(_key(os.path.realpath(dev)))
    except (OSError, ValueError):
        pass
    whys = []
    for p in dict.fromkeys((given, real)):
        k = _key(p)
        if not any(_under(k, d) for d in devs):
            return [WHY_OFF % (text, dev) if p == given
                    else WHY_OFF % ('%s (resolves to %s)' % (text, real), dev)]
        if k in devs:
            return [WHY_ROOT % text]
    for p in dict.fromkeys((given, real)):
        found = _checkout_above(p)
        if found:
            return [WHY_INSIDE % (text, found)]
    for p in dict.fromkeys((given, real)):
        k = _key(p)
        for label, place in places:
            for pk in _place_keys(place):
                if _under(k, pk):
                    whys.append(WHY_OWNER % (text, label, place))
                elif _under(pk, k):
                    whys.append(WHY_HOLDS_OWNER % (text, label, place))
    if whys:
        return list(dict.fromkeys(whys))
    if os.path.isdir(real):
        try:
            hit = _scan(real, cap)
        except OSError as e:
            return [WHY_UNREAD % (text, e)]
        if hit:
            return [hit[0] % (text, hit[1])]
    return []


def check(values, tokens, cfg, kit_root, home=None, environ=None, scan_cap=SCAN_CAP):
    """The whys against acting on the paths a run names (the module docstring): `values` the
    filled arguments {name: value}, `tokens` the filled command after its program, `cfg` the
    machine config user_config returned, `home` the home folder (default the user's). Empty: every
    path may be acted on."""
    dev = kuser.dev_drive(cfg)
    if not dev:
        return [WHY_NO_DEV]
    h = home or kuser.home_dir()
    places, whys = owner_only(cfg, h, kit_root, environ)
    if whys:
        return whys
    for text in candidates(values, tokens):
        whys.extend(_judge(text, dev, places, scan_cap))
    return whys
