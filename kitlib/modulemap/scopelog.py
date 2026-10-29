"""Scope records and the weekly sum (docs/module-map.md "Scope records and the weekly sum").

A gate step that runs `module_map.py --scope --record` keeps the change's Scope (scope.py) as one
small record in `<dev_drive>/pm-kit/kit-gate-scope/<repo key>/`: a sibling of the gate's other
per-repository folders (kitlib.gate.result.kit_folder and repo_key, one folder for every worktree
of a repository), outside every checkout. The weekly maintenance lint (kitlib.maintlint) sums the
last DAYS days of them, so how often gates widened beyond the changed modules, and by which edge,
shows without an audit. The sum refuses and files nothing: a widening the map allows is no
defect.

  folder(root)                       `(the record folder, None)` or `(None, why)`
  write(root, scope, base, mb, now)  `(path, None)` or `(None, why)`: one record, then prune
  prune(folder, now)                 removes records (and stale temp files) older than RETAIN_DAYS
  summary(root, now, days)           `(dict or None, notes)`: None, and no note, when `.kit.json`
                                     names no module map

A record (SCHEMA 1), one JSON file per gate run, `<YYYYMMDDTHHMMSSZ>-<pid>-<4 hex>.json`:

  schema       1
  at           when it was written (UTC, YYYY-MM-DDTHH:MM:SSZ)
  head         HEAD's commit, or null when git cannot say
  branch       the branch checked out, or null (a detached HEAD: a landing's or the daily run's)
  base         the rev the change was measured from, as given or defaulted
  merge_base   the merge base of that rev and HEAD
  paths        {changed path: module}, the first MAX_PATHS in path order
  paths_total  how many paths changed
  changed      the changed modules, sorted
  widen        [{"module", "from", "why"}]: a module the change widens to, the changed module it
               reaches, and scope.scope_of's reason (via edge KIND: WHY, via ratchet ID, via
               messaging)
  widened      true when `widen` is not empty

A change with no changed path writes no record. Each record is written to a dot-named temp file
in the folder, then moved into place (kitlib.fs.replace), so parallel gates never share a file and
a reader sees whole records only.
"""
import datetime
import json
import os
import re
import secrets

from kitlib.config import load as load_config
from kitlib.config.types import ConfigError
from kitlib.fs import replace as fsreplace
from kitlib.gate import result as gresult
from kitlib.git import refs as gitrefs
from kitlib.git import repo as gitrepo

DIR_NAME = 'kit-gate-scope'
SCHEMA = 1
MAX_PATHS = 200         # the paths a record names; paths_total counts them all
RETAIN_DAYS = 28        # a write removes records older than this
DAYS = 7                # the window the weekly sum reads
STAMP = '%Y%m%dT%H%M%SZ'
SECTION = 'modules'
_NAME = re.compile(r'(\d{8}T\d{6}Z)-\d+-[0-9a-f]{4}\.json')
_TEMP = re.compile(r'\.(.+)\.tmp')


def _utc(now):
    return now or datetime.datetime.now(datetime.timezone.utc)


def folder(root):
    """`(<dev_drive>/pm-kit/kit-gate-scope/<repo key>, None)` for the repository `root` is in, or
    `(None, why)`: no git common dir, no dev_drive, or a machine config that cannot be read."""
    common, why = gitrepo.common_dir(root)
    if not common:
        return None, 'no git common dir for %s: %s' % (root, why)
    try:
        base = gresult.kit_folder(DIR_NAME, 'gate scope folder')
    except gresult.ResultError as e:
        return None, ' '.join(str(e).split())
    return os.path.join(base, gresult.repo_key(common)), None


def stamp_of(name):
    """The UTC time a record's file name (or its temp file's) carries, or None."""
    m = _TEMP.fullmatch(name)
    m = _NAME.fullmatch(m.group(1) if m else name)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1), STAMP).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def record_of(root, s, base, merge_base, now=None):
    """The record (module docstring) of the Scope `s` of the checkout at `root`."""
    head, _why = gitrefs.head(root)
    branch, _why = gitrefs.current_branch(root)
    paths = list(s.paths.items())
    return {'schema': SCHEMA,
            'at': _utc(now).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'head': head or None,
            'branch': branch or None,
            'base': base,
            'merge_base': merge_base,
            'paths': dict(paths[:MAX_PATHS]),
            'paths_total': len(paths),
            'changed': list(s.changed),
            'widen': [{'module': m, 'from': src, 'why': why} for m, src, why in s.widen],
            'widened': bool(s.widen)}


def write(root, s, base, merge_base, now=None):
    """Keeps the Scope `s` as one record, then prunes the folder. `(path, None)`, or `(None,
    why)` when no record was written (no changed path, no folder, a write that failed)."""
    if not s.paths:
        return None, 'no changed path'
    now = _utc(now)
    where, why = folder(root)
    if where is None:
        return None, why
    rec = record_of(root, s, base, merge_base, now)
    name = '%s-%d-%s.json' % (now.strftime(STAMP), os.getpid(), secrets.token_hex(2))
    path = os.path.join(where, name)
    tmp = os.path.join(where, '.%s.tmp' % name)
    try:
        os.makedirs(where, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(rec, fh, indent=1, sort_keys=True)
            fh.write('\n')
        fsreplace.replace(tmp, path)
    except OSError as e:
        try:
            fsreplace.remove(tmp)
        except OSError:
            pass
        return None, ' '.join(str(e).split())
    prune(where, now)
    return path, None


def prune(where, now=None):
    """Removes the records, and the temp files, whose name's time is more than RETAIN_DAYS before
    `now`. Any other name is left. The number removed; a file that cannot be removed is left for
    the next write."""
    since = _utc(now) - datetime.timedelta(days=RETAIN_DAYS)
    try:
        names = os.listdir(where)
    except OSError:
        return 0
    gone = 0
    for name in names:
        at = stamp_of(name)
        if at is None or at >= since:
            continue
        try:
            fsreplace.remove(os.path.join(where, name))
            gone += 1
        except OSError:
            pass
    return gone


def summary(root, now=None, days=DAYS):
    """The sum of the records of the last `days` days for the checkout at `root`, as `(dict,
    notes)`; `(None, [])` when `.kit.json` names no module map, `(None, [note])` when the config
    or the folder cannot be read. The dict:

      days, runs (records read), heads (distinct heads), widened (runs that widened),
      widened_heads, routes: [{"from", "why", "modules", "runs"}], most runs first
    """
    try:
        cfg = load_config(root)
    except (ConfigError, OSError) as e:
        return None, ['module scope NOT CHECKED: .kit.json: %s' % ' '.join(str(e).split())]
    if not cfg.section(SECTION).get('map'):
        return None, []
    now = _utc(now)
    where, why = folder(root)
    if where is None:
        return None, ['module scope NOT CHECKED: %s' % why]
    try:
        names = sorted(os.listdir(where))
    except FileNotFoundError:
        names = []
    except OSError as e:
        return None, ['module scope NOT CHECKED: %s' % ' '.join(str(e).split())]
    since = now - datetime.timedelta(days=days)
    runs = widened = bad = 0
    heads, widened_heads, routes = set(), set(), {}
    for name in names:
        if not _NAME.fullmatch(name):
            continue
        at = stamp_of(name)
        if at is None or not since <= at <= now:
            continue
        try:
            with open(os.path.join(where, name), encoding='utf-8') as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            bad += 1
            continue
        if not isinstance(rec, dict) or rec.get('schema') != SCHEMA:
            bad += 1
            continue
        runs += 1
        head = rec.get('head') or name
        heads.add(head)
        widen = [w for w in rec.get('widen') or () if isinstance(w, dict)]
        if not widen:
            continue
        widened += 1
        widened_heads.add(head)
        seen = set()
        for w in widen:
            key = (str(w.get('from')), str(w.get('why')))
            r = routes.setdefault(key, {'from': key[0], 'why': key[1], 'modules': set(),
                                        'runs': 0})
            r['modules'].add(str(w.get('module')))
            if key not in seen:
                seen.add(key)
                r['runs'] += 1
    notes = []
    if not runs:
        notes.append('module scope: no gate scope record in the last %d days: does a gate step '
                     'run module_map.py --scope --record? (docs/module-map.md "Scope records and '
                     'the weekly sum")' % days)
    if bad:
        notes.append('module scope: %d record(s) could not be read' % bad)
    out = sorted(({'from': r['from'], 'why': r['why'], 'modules': sorted(r['modules']),
                   'runs': r['runs']} for r in routes.values()),
                 key=lambda r: (-r['runs'], r['from'], r['why']))
    return {'days': days, 'runs': runs, 'heads': len(heads), 'widened': widened,
            'widened_heads': len(widened_heads), 'routes': out}, notes


def line(s):
    """One line for the sum `s` (a summary dict)."""
    pct = ' (%d%%)' % round(100.0 * s['widened'] / s['runs']) if s['runs'] else ''
    return ('module scope: %d gate run(s) in %d days (%d head(s)), %d widened beyond the changed '
            'modules%s' % (s['runs'], s['days'], s['heads'], s['widened'], pct))
