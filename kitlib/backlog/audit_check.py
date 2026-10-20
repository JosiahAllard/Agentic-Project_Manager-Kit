"""The audit check (tools/backlog_audit.py; docs/backlog.md "Audit log"): the backlog store's
history read against the audit log kitlib.backlog.audit writes, for the PM's digest. Read-only:
it writes nothing to the store, the log or the checkout (a ref store's read fetches, as every
backlog read does, unless `fetch` is false).

A store change is a non-merge commit in the store's history, inside the window, that adds,
changes or deletes a record file: on a ref or branch store the commits reachable from the
snapshot's tip under backlog/, on a folder store in the PM data worktree (MainBackend
commit_branch) HEAD's under the folder. A folder store anywhere else commits nothing per write,
so there is nothing to match: NotChecked.

Each change's command is rebuilt from its commit (the subject's verb, SUBJECTS; the record before
and after it; the message's `Ruling:` line) and sorted by kitlib.backlog.policy.sort, the tool's
own sort, so roles.py alone says which changes must have a line (audit.audited). A change is
covered when a log line's `store_sha` is its commit. The log is the machine's, every repository's
lines in one file, so a line that names no commit here is someone else's and flags nothing; a
line whose sha is MISSING cannot be matched and is only counted.

Findings: UNAUDITED (an AUDITED change no line covers), UNKNOWN (a store commit whose subject no
SUBJECTS entry reads), UNMAPPED (a line inside the window whose role is MISSING or not in
roles.ROLES; every repository's lines). Not findings: a change by a session in BY_DESIGN, whose
writes add no line by design (the landing's close, the Action Hub's writes; docs/backlog.md
"Audit log", Known limit), is counted.

  read_log(path)                  ([(line number, fields)], notes); a missing file is no lines
  verb_of(subject)                the backlog.py verb a store commit's subject names, or None
  history(root, rev, rel, since)  [Change] newest first
  store_place(backend, rules)     (root, rev or None, rel); NotChecked for a folder store elsewhere
  check(backend, rules, entries, since_epoch)   a Report
  run(root, days, fetch, home, now, show_all)   (exit code, lines) for the tool
"""
import collections
import datetime
import json
import os

from kitlib.git import batch, cmd

from . import audit, policy, roles
from . import store as smod
from .errors import Failure, Refusal

OK, FINDINGS, NOT_CHECKED = 0, 1, 2

# The sessions whose store writes add no line by design: kitlib.backlog.at_landing.SESSION and
# kitlib.hubsite.recordwrite.SESSION (tests/backlog/test_audit.py holds them equal).
BY_DESIGN = ('gate-land', 'owner-hub')

# (subject prefix, verb): kitlib.backlog.plan's, migrate's, retarget's and store.catch_up's subjects.
SUBJECTS = (
    ('closes ', 'close'), ('retires ', 'retire'), ('deletes ', 'delete'),
    ('migrate: ', 'migrate'),
    ('backlog: add ', 'add'),
    ('backlog: set ', 'set'), ('backlog: re-cite ', 'set'), ('backlog: priority ', 'set'),
    ('backlog: hold release ', 'set'),
    ('backlog: block ', 'block'), ('backlog: unblock ', 'unblock'),
    ('backlog: park ', 'set-status'), ('backlog: re-park ', 'set-status'),
    ('backlog: unpark ', 'unpark'),
    ('backlog: clear review ', 'clear-review'),
    ('backlog: pull ', 'pull'), ('backlog: release ', 'release'),
    ('backlog: retarget ', 'retarget'), ('backlog: catch up ', 'catch-up'),
)
# Keys only a verb writes (kitlib.backlog.plan.WRITTEN_BY): a `set` that changed them did so
# itself, so they say nothing about what it was given.
_SET_WRITES = ('priority_ruling', 'hold_ruling')

Change = collections.namedtuple('Change', 'sha epoch subject body paths')
Finding = collections.namedtuple('Finding', 'kind text')


class NotChecked(Exception):
    """The check cannot run: why is the message."""


class Report(object):
    def __init__(self, where):
        self.where = where
        self.changes = 0
        self.audited = 0
        self.covered = 0
        self.by_design = collections.Counter()
        self.findings = []
        self.listed = []           # --all: the covered and by-design changes
        self.notes = []
        self.lines_read = 0
        self.no_sha = 0


def utc_of(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


def read_log(path):
    """`([(line number, fields dict)], notes)`: every JSON object line of the log at `path`. A
    missing file is no lines and a note; a line that is not a JSON object is skipped with a note."""
    if not os.path.isfile(path):
        return [], ['no audit log at %s yet: every change that needs a line is flagged' % path]
    entries, notes = [], []
    with open(path, encoding='utf-8', errors='replace') as fh:
        for n, raw in enumerate(fh, 1):
            if not raw.strip():
                continue
            try:
                fields = json.loads(raw)
            except ValueError:
                fields = None
            if not isinstance(fields, dict):
                notes.append('%s:%d is not a JSON object line; skipped' % (path, n))
                continue
            entries.append((n, fields))
    return entries, notes


def verb_of(subject):
    for prefix, verb in SUBJECTS:
        if subject.startswith(prefix):
            return verb
    return None


def history(root, rev, rel, since):
    """[Change] for each non-merge commit reachable from `rev` that touched `<rel>/*.md`, committed
    at or after the epoch `since`, newest first. Failure when git cannot answer."""
    rel = rel.replace('\\', '/').strip('/')
    out, why = cmd.run_text(['log', '--no-merges', '--no-renames', '--since=@%d' % int(since),
                             '--format=%x1e%H%x1f%ct%x1f%s%x1f%b%x1d', '--name-status', rev,
                             '--', rel + '/*.md'], root, strip=False)
    if out is None:
        raise Failure('cannot read the store history: %s' % why)
    changes = []
    for chunk in out.split('\x1e')[1:]:
        head, _, files = chunk.partition('\x1d')
        sha, epoch, subject, body = (head.split('\x1f', 3) + ['', '', ''])[:4]
        paths = []
        for line in files.strip().split('\n'):
            status, _, path = line.partition('\t')
            if path:
                paths.append((status.strip(), path.strip()))
        changes.append(Change(sha, int(epoch), subject, body, paths))
    return changes


def store_place(backend, rules):
    """`(root, rev or None, rel)`: where the store's history is. A ref or branch store's snapshot
    tip (None: the store is empty); a folder store's HEAD in the PM data worktree. NotChecked for
    a folder store anywhere else."""
    if isinstance(backend, smod.MainBackend):
        if not backend.commit_branch():
            raise NotChecked('%s is a folder store outside the PM data worktree: its writes commit '
                             'nothing, so there is no store commit for a line to name'
                             % backend.describe())
        return backend.root, 'HEAD', backend.rel
    snap = backend.load(rules)
    return backend.root, snap.tip, smod.STORE_DIR


def _ruled(body):
    return any(line.startswith('Ruling:') for line in body.split('\n'))


def _given(verb, change, before, after):
    """policy.sort's `given` for one record of `change`: `before` and `after` are its Records (None:
    absent on that side)."""
    if verb == 'set':
        keys = set(k for k, _v in (before.pairs() if before else ())) | \
            set(k for k, _v in (after.pairs() if after else ()))
        fields = [k for k in sorted(keys) if k not in _SET_WRITES
                  and (before.all(k) if before else []) != (after.all(k) if after else [])]
        if before is not None and after is not None and before.body != after.body:
            fields.append('body')
        prices = [(after.get('priority') if after else None) or '-'] if 'priority' in fields else []
        recite = change.subject.startswith('backlog: re-cite ')
        return {'fields': fields, 'priority': prices, 'hold_ruling': recite}
    was = set(before.all('blocked_on')) if before else set()
    now = set(after.all('blocked_on')) if after else set()
    if verb == 'block':
        asked = after is not None and after.all('owner_ask') != (before.all('owner_ask') if before
                                                                 else [])
        return {'on': sorted(now - was), 'owner_ask': asked and bool(after.all('owner_ask'))}
    if verb == 'unblock':
        return {'on': sorted(was - now)}
    if verb == 'pull':
        return {'ruling': _ruled(change.body)}
    if verb in ('retarget', 'catch-up'):
        return {'apply': True}
    return {}


def _records(root, change):
    """{path: (before Record or None, after Record or None)} for `change`'s record paths, read in
    one git process."""
    specs = []
    for _status, path in change.paths:
        specs += ['%s^:%s' % (change.sha, path), '%s:%s' % (change.sha, path)]
    got, why = batch.cat_file(specs, root)
    if got is None:
        raise Failure('cannot read the store\'s records at %s: %s' % (change.sha[:10], why))

    def parsed(spec, path):
        hit = got.get(spec)
        if not hit or hit[0] != 'blob':
            return None
        stem = path.rsplit('/', 1)[-1][:-3]
        return smod.parse_blob(stem, hit[1], path)
    return dict((path, (parsed('%s^:%s' % (change.sha, path), path),
                        parsed('%s:%s' % (change.sha, path), path)))
                for _status, path in change.paths)


def rows_of(root, verb, change):
    """The ACTIONS rows (sorted names) `change` took, by policy.sort over each record it wrote."""
    rows = set()
    for before, after in _records(root, change).values():
        rows.update(policy.sort(verb, _given(verb, change, before, after), before))
    return sorted(rows)


def _ids(change):
    return ','.join(sorted(p.rsplit('/', 1)[-1][:-3] for _s, p in change.paths)) or '-'


def check(backend, rules, entries, since):
    """A Report of the store's changes since the epoch `since` against the log `entries`."""
    report = Report(backend.describe())
    root, rev, rel = store_place(backend, rules)
    shas = set()
    report.lines_read = len(entries)
    for _n, fields in entries:
        sha = fields.get('store_sha')
        if not sha or sha == audit.MISSING:
            report.no_sha += 1
        else:
            shas.add(str(sha))
    changes = history(root, rev, rel, since) if rev else []
    for change in changes:
        report.changes += 1
        session = smod.session_of(change.body) or audit.MISSING
        what = '%s %s %s session=%s' % (change.sha[:10], utc_of(change.epoch), _ids(change),
                                        session)
        if change.sha in shas:
            report.covered += 1
            report.listed.append('COVERED %s  %s' % (what, change.subject))
            continue
        if session in BY_DESIGN:
            report.by_design[session] += 1
            report.listed.append('BY DESIGN %s  %s' % (what, change.subject))
            continue
        verb = verb_of(change.subject)
        if verb is None:
            report.findings.append(Finding('UNKNOWN', '%s  %s' % (what, change.subject)))
            continue
        rows = rows_of(root, verb, change)
        if audit.audited(rows):
            report.audited += 1
            report.findings.append(Finding('UNAUDITED', '%s %s rows=%s  %s' % (
                what, verb, ','.join(rows), change.subject)))
    since_utc = utc_of(since)
    for n, fields in entries:
        utc = str(fields.get('utc') or '')
        role = fields.get('role') or audit.MISSING
        if utc >= since_utc and role not in roles.ROLES:
            report.findings.append(Finding('UNMAPPED', '%s %s %s role=%s session=%s store_sha=%s '
                                           '(log line %d)' % (
                                               utc, fields.get('verb') or '-', fields.get('id') or '-',
                                               role, fields.get('session') or '-',
                                               str(fields.get('store_sha') or '-')[:10], n)))
    return report


def report_lines(report, days, show_all=False):
    lines = ['AUDIT CHECK %s, last %s day%s: %d store change%s, %d covered by a line, %d log '
             'line%s read (%d without a store sha)'
             % (report.where, days, '' if days == 1 else 's', report.changes,
                '' if report.changes == 1 else 's', report.covered, report.lines_read,
                '' if report.lines_read == 1 else 's', report.no_sha)]
    lines += ['NOTE: %s' % n for n in report.notes]
    if show_all:
        lines += ['  ' + x for x in report.listed]
    lines += ['%s %s' % (f.kind, f.text) for f in report.findings]
    total = sum(report.by_design.values())
    lines.append('NOT LOGGED BY DESIGN: %d (%s)' % (total, ', '.join(
        '%s %d' % (s, report.by_design[s]) for s in BY_DESIGN)))
    lines.append('FINDINGS %d' % len(report.findings) if report.findings else 'OK')
    return lines


def run(root, days=1, fetch=True, home=None, now=None, show_all=False):
    """`(exit code, lines)`: the whole check for the repository at `root` over the last `days`."""
    from .opener import open_repo
    now = now or datetime.datetime.now(datetime.timezone.utc)
    since = now.timestamp() - days * 86400
    pm_data, why = audit.pm_data_path(home)
    if why:
        return NOT_CHECKED, ['NOT CHECKED: the machine config\'s pm_data cannot be read (%s)' % why]
    if not pm_data:
        return NOT_CHECKED, ['NOT CHECKED: the machine config names no pm_data, so there is no '
                             'audit log to read']
    try:
        top, cfg, rules = open_repo(root)
        backend = smod.open_backend(top, cfg, fetch=fetch)
        entries, notes = read_log(audit.log_path(pm_data))
        report = check(backend, rules, entries, since)
    except NotChecked as exc:
        return NOT_CHECKED, ['NOT CHECKED: %s' % exc]
    except Refusal as exc:
        return NOT_CHECKED, ['NOT CHECKED: %s' % ' '.join(str(exc).split())]
    report.notes = notes
    return (FINDINGS if report.findings else OK), report_lines(report, days, show_all)
