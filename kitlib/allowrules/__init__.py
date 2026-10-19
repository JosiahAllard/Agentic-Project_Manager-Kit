"""The permission rules that let an agent submit a landing without a prompt, for one repository, and
the scan for rules that name a plugin export other than the current one (docs/allow-rules.md).

  derive(checkout, settings, toplevel, lanes_of)  Target: the current export, the repository's main
                                                  checkout and its lanes, or AllowRulesError
  lines(target)                                   {"allow": [...], "deny": [...]} (kitlib.allowrules.rules)
  named_exports(path, root)                       {export folder: rules naming it} of a settings file
  export_state(root, folder, current)             CURRENT, GONE or NOT_CURRENT

Nothing here writes a settings file: the owner pastes the lines (tools/allow_rules.py prints them).
The export folder is always read from the export root's marketplace.json, never spelled here.
"""
import collections
import json
import os
import re

from kitlib.allowrules import rules

Target = collections.namedtuple('Target', 'kit repo sha lanes root')

CURRENT, GONE, NOT_CURRENT = 'current', 'gone', 'not current'
FULL = 'full'                         # kitlib.records.landing.FULL: the lane every repository has
_FOLDER = re.compile(r'^[0-9a-f]{12}$')
_LANE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
# A path a rule cannot spell bare: a space, a quote, a rule or glob wildcard, a shell operator.
_UNSAFE = frozenset(' \t\r\n"\'()*?[];&|$`%<>')
_KEYS = ('allow', 'deny', 'ask')


class AllowRulesError(Exception):
    """A refusal: no lines are printed, with this reason."""


def _slashes(path):
    return os.path.normpath(path).replace('\\', '/')


def _safe(what, path):
    bad = sorted(set(path) & _UNSAFE)
    if bad:
        raise AllowRulesError('%s %s holds %s, which a permission rule cannot spell'
                              % (what, path, ' '.join(repr(c) for c in bad)))


def repo_lanes(top):
    """The lanes besides full of `top`'s main branch config at HEAD ("landing".lanes, which
    gate.py land --lane checks against)."""
    from kitlib import config
    return config.load_at(top, 'HEAD').section('landing')['lanes']


def derive(checkout, settings=None, toplevel=None, lanes_of=None):
    """The Target for `checkout` (any worktree of the repository): the current export folder of
    `settings` (kitlib.pluginsource.load() when None), the main checkout `toplevel` gives
    (kitlib.git.repo.main_checkout_toplevel) and `lanes_of(main checkout)` (repo_lanes)."""
    from kitlib import pluginsource
    from kitlib.config.types import ConfigError
    if settings is None:
        try:
            settings = pluginsource.load()
        except (pluginsource.RefreshError, ConfigError) as e:
            raise AllowRulesError('the plugin settings: %s' % e)
    root = settings.export_dir
    folder = pluginsource.current_folder(root)
    if not folder or not _FOLDER.match(folder):
        raise AllowRulesError('no current plugin export under %s (%s)' % (
            root, 'marketplace.json names %r, not an export folder' % folder if folder
            else 'no readable marketplace.json'))
    path = os.path.join(root, folder)
    if pluginsource.read_marker(path) is None:
        raise AllowRulesError('no current plugin export under %s (%s has no %s marker)'
                              % (root, folder, pluginsource.MARKER))
    kit = _slashes(path)
    _safe('the plugin export path', kit)
    if toplevel is None:
        from kitlib.git.repo import main_checkout_toplevel as toplevel
    top = toplevel(checkout)
    if not top:
        raise AllowRulesError('git cannot place %s in a repository' % checkout)
    repo = _slashes(top)
    _safe('the main checkout path', repo)
    try:
        lanes = (lanes_of or repo_lanes)(top)
    except ConfigError as e:
        raise AllowRulesError('the lanes of %s: %s' % (repo, e))
    bad = [l for l in lanes if not isinstance(l, str) or not _LANE.match(l)]
    if bad:
        raise AllowRulesError('lane name %s is not letters, digits, "_", "." and "-"'
                              % ', '.join(repr(l) for l in bad))
    return Target(kit, repo, folder, (FULL,) + tuple(sorted(l for l in lanes if l != FULL)),
                  _slashes(root))


def lines(target):
    """The allow and deny lines for `target`, in a fixed order."""
    return {'allow': rules.allow_lines(target.kit, target.repo, target.lanes),
            'deny': rules.deny_lines(target.kit, target.repo)}


def _root_pattern(root):
    parts = [re.escape(p) for p in re.split(r'[\\/]+', os.path.normpath(root)) if p]
    return re.compile(r'(?i:%s)[\\/]+([0-9a-f]{12})(?![0-9A-Za-z])' % r'[\\/]+'.join(parts))


def named_exports(path, root):
    """{export folder: the number of permission rules (allow, deny, ask) naming it} in the settings
    file `path`, for paths under the export root `root` (either slash, any case). None when there
    is no file. OSError or ValueError when it cannot be read as a JSON object."""
    if not os.path.lexists(path):
        return None
    with open(path, encoding='utf-8-sig') as fh:
        data = json.loads(fh.read())
    if not isinstance(data, dict):
        raise ValueError('not a JSON object')
    perms = data.get('permissions')
    if not isinstance(perms, dict):
        return {}
    pattern = _root_pattern(root)
    out = collections.Counter()
    for key in _KEYS:
        entries = perms.get(key)
        for rule in entries if isinstance(entries, list) else ():
            if isinstance(rule, str):
                for folder in set(pattern.findall(rule)):
                    out[folder] += 1
    return dict(out)


def export_state(root, folder, current):
    """CURRENT for the folder marketplace.json names, GONE when `root`/`folder` holds no export
    marker, else NOT_CURRENT."""
    from kitlib import pluginsource
    if folder == current:
        return CURRENT
    if pluginsource.read_marker(os.path.join(root, folder)) is None:
        return GONE
    return NOT_CURRENT
