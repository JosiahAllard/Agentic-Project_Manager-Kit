"""`stale`: records that may no longer say what is true.
Read-only; it reports, it never writes or fails a record.

Three tests, each a different way of going stale:

  age    the store's last commit that wrote the record is `days` or more days old
  body   a backticked repo path in the body names no file in the checkout
  files  a `files` entry changed (or is gone) between the commit `files_at` names and HEAD

A record a test could not judge is listed as NOT CHECKED, never counted as current; the SWEPT line
says how many were judged and how many skipped, so an empty report with skips is a blind one.
"""
import datetime
import os
import posixpath

from kitlib.docs import index as docindex
from kitlib.git import batch, cmd

CHANGED, GONE = 'CHANGED', 'MOVED OR GONE'
DEFAULT_DAYS = 30


class Report(object):
    """What a sweep found: `old` [(id, days)] oldest first; `no_history` ids no commit wrote;
    `bad_paths` [(id, ref)]; `moved` [(id, path, verdict)]; `unchecked` [(id, why)]; the counts
    of `files` entries judged and skipped; `total` records read."""

    def __init__(self, days, total):
        self.days = days
        self.total = total
        self.old = []
        self.no_history = []
        self.bad_paths = []
        self.moved = []
        self.unchecked = []
        self.files_checked = 0
        self.files_skipped = 0

    def lines(self):
        out = ['']
        out.append('UNTOUCHED %d+ DAYS (%d) - the last store commit that wrote the record'
                  % (self.days, len(self.old)))
        out.extend('  %-24s %d days' % pair for pair in self.old)
        out.extend(['  none'] if not self.old else [])
        out.append('')
        out.append('BODY PATHS GONE (%d) - a backticked repo path in the body, no file at that path'
                   % len(self.bad_paths))
        out.extend('  %-24s `%s`' % pair for pair in self.bad_paths)
        out.extend(['  none'] if not self.bad_paths else [])
        out.append('')
        out.append('FILES CHANGED OR MOVED SINCE files_at (%d)' % len(self.moved))
        out.extend('  %-24s %s - %s' % row for row in self.moved)
        out.extend(['  none'] if not self.moved else [])
        if self.unchecked or self.no_history:
            out.append('')
            out.append('NOT CHECKED (%d) - these are not "current", they are unknown:'
                       % (len(self.unchecked) + len(self.no_history)))
            out.extend('  %-24s %s' % row for row in self.unchecked)
            out.extend('  %-24s no store commit wrote it yet, so its age is unknown' % rid
                       for rid in self.no_history)
        out.append('')
        out.append('SWEPT: %d records; %d `files` entries checked, %d skipped.'
                   % (self.total, self.files_checked, self.files_skipped))
        out.append('An empty report above with a non-zero skipped count is a BLIND report, not a '
                   'clean one.')
        return out


def _day(epoch):
    """The commit's local calendar day: `today` (Context.date) is the local day too, so a record
    written late last night is one day old, not zero or two."""
    return datetime.date.fromtimestamp(epoch)


def sweep(snap, touched, today, days, root):
    """The Report for `snap`. `touched` is `{id: epoch}` (`Store.touched`), `today` YYYY-MM-DD,
    `root` the checkout the paths are read in; git failures are noted per record as NOT CHECKED."""
    now = datetime.date.fromisoformat(today)
    recs = snap.readable
    rep = Report(days, len(snap.records))
    for rec in snap.unreadable:
        rep.unchecked.append((rec.stem, 'the record is unreadable, so nothing in it was checked'))
    for rec in recs:
        when = touched.get(rec.stem)
        if when is None:
            rep.no_history.append(rec.stem)
        elif (now - _day(when)).days >= days:
            rep.old.append((rec.stem, (now - _day(when)).days))
        seen = set()
        for ref in docindex.path_refs(rec.body or ''):
            if '/' in ref and ref not in seen:
                seen.add(ref)
                if not os.path.isfile(os.path.join(root, *ref.split('/'))):
                    rep.bad_paths.append((rec.stem, ref))
    rep.old.sort(key=lambda p: (-p[1], p[0]))
    _files(rep, recs, root)
    return rep


def _bad_rev(rev):
    return rev.startswith('-') or any(c in rev for c in '\n\r\0')


def _changed_since(sinces, root):
    """`({since: set of paths}, {since: why})` for resolved `{since: full hash}`: the paths
    written by the commits reachable from HEAD and not from that commit. Two git calls however
    many commits there are: `rev-list --parents HEAD` (the graph) and one `log --name-only` over
    what is not reachable from every `files_at` commit. A path changed and changed back counts as
    changed (a diff would not say so)."""
    graph, why = cmd.run_text(['rev-list', '--parents', 'HEAD', '--'], root, timeout=120)
    if graph is None:
        return {}, dict.fromkeys(sinces, why)
    parents = {}
    for line in graph.split('\n'):
        ids = line.split()
        if ids:
            parents[ids[0]] = ids[1:]

    def closure(commit):
        seen, todo = set(), [commit]
        while todo:
            cur = todo.pop()
            if cur not in seen and cur in parents:
                seen.add(cur)
                todo.extend(parents[cur])
        return seen
    anc = {since: closure(commit) for since, commit in sinces.items()}
    common = set.intersection(*anc.values())
    heads = common - {p for c in common for p in parents[c]}
    data, why = cmd.run_text(['-c', 'core.quotePath=false', 'log', '--no-renames', '--name-only',
                              '--format=%x01%H%x02', 'HEAD', '--not'] + sorted(heads) + ['--'],
                             root, timeout=120, strip=False)
    if data is None:
        return {}, dict.fromkeys(sinces, why)
    names = {header.strip(): paths for header, paths in batch.log_records_names(data)}
    return {since: {n for c, paths in names.items() if c not in seen for n in paths}
            for since, seen in anc.items()}, {}


def _files(rep, recs, root):
    """Fill `moved`, `unchecked` and the file counts. A `files_at` that is not a plain rev is
    skipped alone; the rest are resolved in one call and compared in one history walk."""
    entries = [(rec, path, rec.get('files_at')) for rec in recs for path in rec.all('files')]
    sinces = sorted({s for _r, _p, s in entries if s is not None and not _bad_rev(s)})
    resolved, why = batch.commits(sinces, root) if sinces else ({}, None)
    if resolved is None:
        resolved = {}
    good = {s: c for s, c in resolved.items() if c is not None}
    diffs, failed = _changed_since(good, root) if good else ({}, {})
    for rec, path, since in entries:
        if since is None:
            verdict = 'the record has no `files_at`, so there is no commit to compare with'
        elif _bad_rev(since):
            verdict = '`files_at` is not a commit name'
        elif since not in good:
            verdict = '`files_at: %s` is not a commit here (%s)' % (since, why or 'no such commit')
        elif since in failed:
            verdict = 'git log failed: %s' % failed[since]
        else:
            verdict = None
        if verdict is not None:
            rep.files_skipped += 1
            rep.unchecked.append((rec.stem, 'files: %s - %s' % (path, verdict)))
            continue
        rep.files_checked += 1
        key = posixpath.normpath(path)
        if not os.path.exists(os.path.join(root, *key.split('/'))):
            rep.moved.append((rec.stem, path, GONE))
        elif any(c == key or c.startswith(key + '/') for c in diffs[since]):
            rep.moved.append((rec.stem, path, CHANGED))
