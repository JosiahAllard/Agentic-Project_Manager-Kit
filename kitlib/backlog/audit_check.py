"""The audit check (tools/backlog_audit.py; docs/backlog.md "Audit log"): the backlog store's
history read against the audit log kitlib.backlog.audit writes, for the PM's digest. It writes
no record, no log line and no file in the checkout. With `fetch` (the default) a ref or branch
store is fetched first, as every backlog read is: that rewrites the local store ref, and the
first time it may add the store's fetch refspec to the remote in .git/config
(store.ensure_refspec). With `fetch` false it does neither.

A store change is a non-merge commit in the store's history, inside the window, that adds,
changes or deletes a record file: on a ref or branch store the commits reachable from the
snapshot's tip under backlog/, on a folder store in the PM data worktree (MainBackend
commit_branch) HEAD's under the folder. A folder store anywhere else commits nothing per write,
so there is nothing to match: NotChecked.

Each change's command is rebuilt from its commit (the subject's verb, SUBJECTS; the record before
and after it; a collision the plan marked on the subject, policy.COLLISION_MARK) and sorted by
kitlib.backlog.policy.sort, the tool's own sort, so roles.py alone says which changes must have a line (audit.audited). A change is
covered when a log line's `store_sha` is its commit. The log is the machine's, every repository's
lines in one file, so a line that names no commit here is someone else's and flags nothing; a
line whose sha is MISSING cannot be matched and is only counted.

Findings: UNAUDITED (an AUDITED change no line covers), UNKNOWN (a store commit whose subject no
SUBJECTS entry reads), UNMAPPED (a line inside the window whose role is MISSING or not in
roles.ROLES; every repository's lines). Not findings: a change by a session in BY_DESIGN, whose
writes add no line by design (the landing's close, the Action Hub's writes, the daily full run's
pricing, the hub's unblock of a met `until` condition; docs/backlog.md
"Audit log", Known limit), is counted.

UNVERIFIED (docs/backlog.md "Audit log"): each `answer:` line a change
adds or changes, whatever its session, covered and by-design changes included, is checked. It
passes only when the change was written by the Action Hub (its `Session:` is owner-hub,
ANSWER_WRITER) or is a `catch-up` commit (an answer line a failed hub write left in a folder
store), AND the hub store holds that question with that record id, an `answer.at` equal to or
later than the line's (a re-answer replaced it: held) and a status in ANSWERED. Anything else is
the finding. The window's records are read once, in one git process for every change (records_of),
and shared with rows_of. The hub store is read only, through the hub's own client (`questions`,
default hub_questions), and only when the window holds an answer change the hub wrote; a hub that
cannot be read is a NOT CHECKED note for those lines, never a finding.

  read_log(path)                  ([(line number, fields)], notes); a missing file is no lines
  verb_of(subject)                the backlog.py verb a store commit's subject names, or None
  history(root, rev, rel, since)  [Change] newest first
  records_of(root, changes)       {sha: {path: (before, after)}}, read in one git process
  store_place(backend, rules)     (root, rev or None, rel); NotChecked for a folder store elsewhere
  check(backend, rules, entries, since_epoch, questions)   a Report
  hub_questions()                 every hub question document, read with the agent token
  run(root, days, fetch, home, now, show_all, questions)   (exit code, lines) for the tool
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

# The sessions whose store writes add no line by design: kitlib.backlog.at_landing.SESSION,
# kitlib.hubsite.recordwrite.SESSION, kitlib.dailyfull.file.SLUG and
# kitlib.hubsite.recordwrite.CONDITION_SESSION (the hub's unblock of a met `until` condition;
# tests/backlog/test_audit.py holds them equal).
BY_DESIGN = ('gate-land', 'owner-hub', 'daily-full', 'hub-condition')

# The one writer of an `answer:` line (kitlib.hubsite.recordwrite.SESSION), the commit subject
# that may carry one it left uncommitted (store.catch_up), and the hub question statuses that hold
# the owner's answer (kitlib.hubvalidate STATUSES: answered, filed, then logged or done once its
# asker reads it; a re-answer is told apart by its `at`).
ANSWER = 'UNVERIFIED'
ANSWER_WRITER = 'owner-hub'
CATCH_UP = 'backlog: catch up '
ANSWERED = ('answered', 'filed', 'logged', 'done')

# (subject prefix, verb): kitlib.backlog.plan's (`uncite` among them), retarget's and
# store.catch_up's subjects, and `migrate: `, the subject of a store's first commit in old store
# history (the store migration's; kept so that history stays audited).
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
    ('backlog: retarget ', 'retarget'), ('backlog: uncite ', 'uncite'),
    ('backlog: catch up ', 'catch-up'),
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
    # git reads `--since=@0` as an empty window, so an epoch of 0 or less means no limit.
    window = ['--since=@%d' % int(since)] if int(since) > 0 else []
    out, why = cmd.run_text(['log', '--no-merges', '--no-renames'] + window +
                            ['--format=%x1e%H%x1f%ct%x1f%s%x1f%b%x1d', '--name-status', rev,
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
        priced_by = (after.all('priority_by') if after else []) if 'priority_by' in fields else []
        return {'fields': fields, 'priority': prices, 'priority_by': priced_by,
                'blocked_on': after.all('blocked_on') if after else []}
    was = set(before.all('blocked_on')) if before else set()
    now = set(after.all('blocked_on')) if after else set()
    if verb == 'block':
        asked = after is not None and after.all('owner_ask') != (before.all('owner_ask') if before
                                                                 else [])
        return {'on': sorted(now - was), 'owner_ask': asked and bool(after.all('owner_ask'))}
    if verb == 'unblock':
        return {'on': sorted(was - now)}
    if verb in ('retarget', 'uncite', 'catch-up'):
        return {'apply': True}
    return {}


def records_of(root, changes):
    """{sha: {path: (before Record or None, after Record or None)}} for every change's record
    paths, all read in one git process (so a covered or by-design change, whose answer lines are
    checked too, starts none of its own)."""
    specs = []
    for change in changes:
        for _status, path in change.paths:
            specs += ['%s^:%s' % (change.sha, path), '%s:%s' % (change.sha, path)]
    if not specs:
        return dict((change.sha, {}) for change in changes)
    got, why = batch.cat_file(specs, root)
    if got is None:
        raise Failure('cannot read the store\'s records at %s: %s'
                      % (','.join(c.sha[:10] for c in changes), why))

    def parsed(spec, path):
        hit = got.get(spec)
        if not hit or hit[0] != 'blob':
            return None
        stem = path.rsplit('/', 1)[-1][:-3]
        return smod.parse_blob(stem, hit[1], path)
    return dict((change.sha, dict((path, (parsed('%s^:%s' % (change.sha, path), path),
                                          parsed('%s:%s' % (change.sha, path), path)))
                                  for _status, path in change.paths))
                for change in changes)


def _records(root, change):
    """{path: (before Record or None, after Record or None)} for `change`'s record paths, read in
    one git process."""
    return records_of(root, [change])[change.sha]


def rows_of(root, verb, change, records=None):
    """The ACTIONS rows (sorted names) `change` took, by policy.sort over each record it wrote,
    and the collision row a plan found when the subject carries policy.COLLISION_MARK.
    `records`: _records' answer for `change` when already read (None: read it)."""
    rows = set()
    for before, after in (records if records is not None else _records(root, change)).values():
        rows.update(policy.sort(verb, _given(verb, change, before, after), before))
    if verb in policy.COLLISION_ROWS and policy.COLLISION_MARK in change.subject:
        rows.add(policy.COLLISION_ROWS[verb])
    return sorted(rows)


def _ids(change):
    return ','.join(sorted(p.rsplit('/', 1)[-1][:-3] for _s, p in change.paths)) or '-'


def answer_lines(root, change, records=None):
    """[(record id, `<qid> <at>`)] for each `answer:` line `change` adds or changes; a removed line
    is none (docs/backlog.md "Audit log", Known limits). `records`: as rows_of's."""
    out = []
    got = records if records is not None else _records(root, change)
    for path, (before, after) in sorted(got.items()):
        if after is None:
            continue
        was = set(before.all('answer')) if before is not None else set()
        rid = path.rsplit('/', 1)[-1][:-3]
        out += [(rid, v) for v in after.all('answer') if v not in was]
    return out


def hub_questions():
    """Every question document on this machine's hub, read with the agent token through the hub's
    client (as kitlib.maintlint.backlog hub_questions and tools/hub.py read them): read only.
    Raises what the client raises, and ConfigError for a hub config that cannot be read."""
    from kitlib.hubsite import client, settings
    return client.Client(settings.load().port).listing('questions', status='all',
                                                         role=client.AGENT)


def hub_answers(docs):
    """{(qid, record id, at)} of the hub's answered questions bound to a record (status in
    ANSWERED). The record's repo is not matched (docs/backlog.md "Audit log", Known limits)."""
    out = set()
    for d in docs or ():
        if not isinstance(d, dict) or d.get('status') not in ANSWERED:
            continue
        record, ans = d.get('record'), d.get('answer')
        if isinstance(record, dict) and isinstance(ans, dict):
            out.add((str(d.get('id')), str(record.get('id')), str(ans.get('at'))))
    return out


def _when(at):
    """The aware datetime of an answer time, or None when it is not one."""
    try:
        when = datetime.datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo is not None else None


def held(answers, qid, rid, at):
    """True when the hub's `answers` (hub_answers) hold question `qid` on record `rid` answered at
    `at`, or at a later time: a re-answer replaced the line `at` (plan_answer), so the hub keeps
    only the newer `at` (docs/backlog.md "Audit log", Known limits)."""
    if (qid, rid, at) in answers:
        return True
    when = _when(at)
    return when is not None and any(
        q == qid and r == rid and _when(a) is not None and _when(a) > when
        for q, r, a in answers)


def _hub_read(questions):
    """(the hub's answers, None) or (None, why the hub store could not be read)."""
    from kitlib.config.types import ConfigError
    from kitlib.hubsite import serve
    try:
        return hub_answers((questions or hub_questions)()), None
    except (serve.ServeError, ConfigError, OSError, ValueError) as exc:
        return None, '%s: %s' % (type(exc).__name__, ' '.join(str(exc).split()))


def check(backend, rules, entries, since, questions=None):
    """A Report of the store's changes since the epoch `since` against the log `entries`.
    `questions`: a callable giving the hub's question documents (None: hub_questions), called
    once, only when a change in the window that the hub wrote adds an answer line."""
    report = Report(backend.describe())
    root, rev, rel = store_place(backend, rules)
    hub = []                                 # [answers or None, why], read on first need
    shas = set()
    report.lines_read = len(entries)
    for _n, fields in entries:
        sha = fields.get('store_sha')
        if not sha or sha == audit.MISSING:
            report.no_sha += 1
        else:
            shas.add(str(sha))
    changes = history(root, rev, rel, since) if rev else []
    every = records_of(root, changes) if changes else {}
    for change in changes:
        report.changes += 1
        session = smod.session_of(change.body) or audit.MISSING
        what = '%s %s %s session=%s' % (change.sha[:10], utc_of(change.epoch), _ids(change),
                                        session)
        records = every[change.sha]
        answers = answer_lines(root, change, records)
        writer = session == ANSWER_WRITER or change.subject.startswith(CATCH_UP)
        if answers and writer and not hub:
            hub[:] = _hub_read(questions)
            if hub[1]:
                report.notes.append('answers NOT CHECKED: the hub store could not be read (%s), '
                                    'so no answer line the hub wrote was checked' % hub[1])
        for rid, line in answers:
            qid, _, at = line.partition(' ')
            if not writer:
                why = 'not written by %s' % ANSWER_WRITER
            elif hub[0] is None or held(hub[0], qid, rid, at):
                continue
            else:
                why = 'no answered or filed hub question %s on %s at %s' % (qid, rid, at)
            report.findings.append(Finding(ANSWER, '%s answer: %s (%s)  %s' % (
                what, line, why, change.subject)))
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
        rows = rows_of(root, verb, change, records)
        if not rows:                         # a write subject no roles.py row names yet
            report.findings.append(Finding('UNKNOWN', '%s %s rows=none  %s' % (
                what, verb, change.subject)))
            continue
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


def run(root, days=1, fetch=True, home=None, now=None, show_all=False, questions=None):
    """`(exit code, lines)`: the whole check for the repository at `root` over the last `days`
    (`questions`: check's)."""
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
        report = check(backend, rules, entries, since, questions)
    except NotChecked as exc:
        return NOT_CHECKED, ['NOT CHECKED: %s' % exc]
    except Refusal as exc:
        return NOT_CHECKED, ['NOT CHECKED: %s' % ' '.join(str(exc).split())]
    except (OSError, ValueError) as exc:          # an unreadable log or store: never FINDINGS
        return NOT_CHECKED, ['NOT CHECKED: cannot read the log or the store: %s: %s'
                             % (type(exc).__name__, ' '.join(str(exc).split()))]
    report.notes = notes + report.notes
    return (FINDINGS if report.findings else OK), report_lines(report, days, show_all)
