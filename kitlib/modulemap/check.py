"""The edge check and the baseline (docs/module-map.md "--check", "--baseline").

The check scans the whole tree each run, so it keeps no state and needs no diff. Each crossing a
detector reports is, in this order: into a messaging module (allowed), along an allowed edge of its
kind (allowed), counted by a ratchet, or refused. A ratchet whose count is over its ceiling fails
too; one under its ceiling gets a "lower the ceiling" line.
"""
import collections
import json
import os
import re

from kitlib.git import cmd as gitcmd

from . import detectors
from .load import ROOT, MapError, load_ceilings, load_map

SECTION = 'modules'
LIST_TIMEOUT = 120
MAX_BYTES = 4 * 1024 * 1024
OK, FAIL, REFUSED = 0, 1, 2
Outcome = collections.namedtuple('Outcome', 'code lines')
Sorted = collections.namedtuple('Sorted', 'messaging allowed ratcheted refused')


def tree_files(root):
    """The repo-relative files git lists in `root` (tracked, and untracked but not ignored).
    MapError when git cannot list them: an empty list would read as a clean tree."""
    r = gitcmd.call(['ls-files', '-co', '--exclude-standard', '-z'], root, timeout=LIST_TIMEOUT)
    if r.rc != 0:
        raise MapError('git ls-files failed in %s: %s' % (root, r.err or 'exit %s' % r.rc))
    return sorted(set(n for n in gitcmd.text(r.out).split('\0') if n))


def reader(root):
    def read(rel):
        path = os.path.join(root, *rel.split('/'))
        try:
            if os.path.getsize(path) > MAX_BYTES:
                return None
            with open(path, encoding='utf-8-sig', errors='replace') as fh:
                return fh.read()
        except OSError:
            return None
    return read


def _abs(root, rel):
    return os.path.join(root, *rel.replace('\\', '/').split('/'))


def map_paths(root, cfg, map_path=None, ceilings_path=None, require_owner=True):
    """`(map file, ceilings file or None, notes)`, absolute. Given paths win over `.kit.json`
    "modules"; with none, the section must name the map, and (`require_owner`) the owner rules
    must lock it. MapError otherwise."""
    notes = []
    sec = cfg.section(SECTION)
    if map_path and require_owner:
        notes.append('dry run: the map was given on the command line, so it is not checked as an '
                     'owner path')
    elif not map_path:
        rel = sec.get('map')
        if not rel:
            raise MapError('.kit.json has no "modules".map naming the module map '
                           '(docs/module-map.md "Configuring")')
        if require_owner and not cfg.owner_rules().is_locked(rel):
            raise MapError('the module map %s is not an owner path: list it in .kit.json '
                           '"owner".paths (docs/module-map.md "Configuring")' % rel)
        map_path = _abs(root, rel)
    if not ceilings_path and sec.get('ceilings'):
        ceilings_path = _abs(root, sec['ceilings'])
    return map_path, ceilings_path, notes


def scan(mm, files, read):
    """Every crossing the detectors report, sorted by file and line."""
    out = []
    for d in detectors.all_detectors():
        out.extend(d.detect(files, mm, read))
    return sorted(out, key=lambda c: (c.path, c.line, c.detector, c.dst, c.target))


def sort_crossings(mm, crossings):
    messaging, allowed, refused = [], [], []
    ratcheted = collections.OrderedDict((r['id'], []) for r in mm.ratchets)
    for c in crossings:
        if c.dst in mm.messaging:
            messaging.append(c)
        elif mm.edge_for(c.src, c.dst, c.kind):
            allowed.append(c)
        else:
            r = mm.ratchet_for(c)
            if r:
                ratcheted[r['id']].append(c)
            else:
                refused.append(c)
    return Sorted(messaging, allowed, ratcheted, refused)


def where(c):
    return '%s:%d  (%s: %s)' % (c.path, c.line, c.detector, c.target)


def check(mm, ceilings, files, read):
    """Outcome of the edge check: 0 every crossing allowed or under its ratchet's ceiling, 1 not."""
    s = sort_crossings(mm, scan(mm, files, read))
    lines, failed = [], False
    total = len(s.messaging) + len(s.allowed) + sum(map(len, s.ratcheted.values())) + len(s.refused)
    lines.append('module_map: %d crossing(s): %d into messaging, %d on allowed edges, %d under '
                 'ratchets, %d refused' % (total, len(s.messaging), len(s.allowed),
                                           sum(map(len, s.ratcheted.values())), len(s.refused)))
    for c in s.refused:
        failed = True
        lines.append('REFUSED  %s -> %s  %s  %s' % (c.src, c.dst, c.kind, where(c)))
    unknown = sorted(k for k in ceilings if k not in s.ratcheted)
    for k in unknown:
        lines.append('note: the ceilings file names %s, which no ratchet in the map has' % k)
    for r in mm.ratchets:
        got = s.ratcheted[r['id']]
        ceiling = ceilings.get(r['id'], 0)
        state = 'ok'
        if len(got) > ceiling:
            failed = True
            state = 'OVER CEILING'
        elif len(got) < ceiling:
            state = 'lower the ceiling to %d' % len(got)
        lines.append('ratchet %s  %s -> %s  count %d / ceiling %d / goal %d  %s'
                     % (r['id'], r['from'], r['to'], len(got), ceiling, r['goal'], state))
        if len(got) > ceiling:
            for c in got:
                lines.append('  %s' % where(c))
    lines.append('module_map: %s' % ('FAIL' if failed else 'OK'))
    return Outcome(FAIL if failed else OK, lines)


def ratchet_id(src, kind, dst):
    name = '%s-%s-%s' % (src, kind, 'root' if dst == ROOT else dst)
    return re.sub(r'[^A-Za-z0-9._-]', '-', name)


def baseline(mm, ceilings, files, read):
    """Outcome (always 0) printing the ratchets the current tree needs: the map's ratchets with
    their counts as ceilings, and one new ratchet per (from, kind, to) of refused crossings."""
    s = sort_crossings(mm, scan(mm, files, read))
    new = collections.OrderedDict()
    for c in s.refused:
        new.setdefault((c.src, c.kind, c.dst), []).append(c)
    taken = {r['id'] for r in mm.ratchets}
    entries, ceil = [], collections.OrderedDict()
    lines = ['module_map baseline: %d existing ratchet(s), %d new' % (len(mm.ratchets), len(new))]
    for r in mm.ratchets:
        n = len(s.ratcheted[r['id']])
        ceil[r['id']] = n
        lines.append('  %-32s %s -> %s  count %d (ceiling now %d)'
                     % (r['id'], r['from'], r['to'], n, ceilings.get(r['id'], 0)))
    for (src, kind, dst), got in new.items():
        rid = ratchet_id(src, kind, dst)
        while rid in taken:
            rid += '-x'
        taken.add(rid)
        ceil[rid] = len(got)
        entries.append(collections.OrderedDict([('id', rid), ('from', src), ('to', dst),
                                                ('kinds', [kind]), ('goal', 0)]))
        lines.append('  %-32s %s -> %s  %s  count %d (new)' % (rid, src, dst, kind, len(got)))
    if entries:
        lines.append('new ratchets for the map (the owner sets each goal; a goal above 0 needs '
                     'goal_reason):')
        lines.extend(json.dumps(e) for e in entries)
    lines.append('ceilings:')
    lines.append(json.dumps(ceil, indent=1))
    return Outcome(OK, lines)


def run(root, cfg, mode, map_path=None, ceilings_path=None, files=None):
    """`mode` 'check' or 'baseline'. MapError when the map, the ceilings file or the tree listing
    cannot be used. A baseline needs no owner path (it writes nothing)."""
    map_path, ceilings_path, notes = map_paths(root, cfg, map_path, ceilings_path,
                                               require_owner=(mode == 'check'))
    mm = load_map(map_path)
    ceilings = load_ceilings(ceilings_path)
    if files is None:
        files = tree_files(root)
    fn = check if mode == 'check' else baseline
    out = fn(mm, ceilings, files, reader(root))
    return Outcome(out.code, notes + out.lines)
