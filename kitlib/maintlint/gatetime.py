"""kitlib.maintlint.gatetime: the gate-time check, and the one record maint_lint files
(docs/maint-lint.md "Gate time").

The timings are the steps of the repository's gate-OK records (kitlib.records.gate_ok,
`<dev_drive>/pm-kit/kit-gate-ok/<repo key>/<commit>.json`, or with no dev_drive the .git
copies in `<git common dir>/kit-gate-ok/`), of full gates only (mode_of "full": `gate.py
--full` and full landings; a selected or docs-only gate times a subset of a step). A sample is a
step whose result is pass or warn with seconds above 0; reused and skipped steps did not run.

Per step: the newest WINDOW samples that ended in the last DAYS days. With MIN_SAMPLES or more,
a median over `.kit.json` "maint_lint".gate_time.bound_s (default BOUND_S) is a `gate-time`
finding whose target is the step name. Fewer samples is a note, never a finding.

  settings(root)                     `(conf, None)` or `(None, why)`: prefix, bound_s, session
  samples(root, now)                 `({step: [seconds, newest first]}, None)` or `(None, why)`
  findings(root, now)                `(findings, notes)` for kitlib.maintlint.run
  file(root, result, now, opener)    a plain run's filing: one record per gate-time finding
                                     through kitlib.dailyfull.file.open_filing_store, never a
                                     second while a live record of that step is in the store;
                                     the lines to print

file() is the only write in maint_lint, and only tools/maint_lint.py's plain run calls it.
Nothing here raises for a store, a config or a record that cannot be read: each is a line.
"""
import datetime
import json
import os
import re
import statistics

from kitlib.backlog import plan as pmod
from kitlib.backlog import scope as bscope
from kitlib.backlog import store as bstore
from kitlib.backlog.errors import Failure, Refusal
from kitlib.config import load as load_config
from kitlib.config.types import ConfigError
from kitlib.records import gate_ok

from . import Finding

CHECK = 'gate-time'
WINDOW = 10                 # the newest samples per step the median is taken over
DAYS = 14                   # how far back a sample may have ended
MIN_SAMPLES = 5             # fewer: a note, never a finding
BOUND_S = 900               # the schema's default for "maint_lint".gate_time.bound_s
SLUG = 'maint-lint'         # the store commit's Session: line
REF = 'maint-lint gate-time %s'
NOT_FOLDED = ('found by the maint-lint gate-time check on the main branch, with no branch to '
              'fold it into')
TIMED = ('pass', 'warn')


def settings(root):
    """The "maint_lint".gate_time settings of the checkout at `root`, every key present."""
    try:
        conf = load_config(root).section('maint_lint').get('gate_time') or {}
    except (ConfigError, OSError) as exc:
        return None, '.kit.json: %s' % ' '.join(str(exc).split())
    return {'prefix': conf.get('prefix'), 'bound_s': conf.get('bound_s') or BOUND_S,
            'session': conf.get('session')}, None


def _ended(record):
    try:
        return datetime.datetime.strptime(record.get('ended_utc'), '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def samples(root, now):
    """Each step's sample seconds, newest first, at most WINDOW, from the full gate-OK records
    that ended in the last DAYS days before `now`. A record that cannot be read is skipped."""
    try:
        try:
            folder = gate_ok.record_dir(root)
        except gate_ok.NoRecordFolder:
            # Kept for load nodes set up without a dev_drive (docs/load-nodes.md).
            folder = gate_ok.legacy_dir(root)
        paths = {n: os.path.join(folder, n) for n in os.listdir(folder)
                 if gate_ok.is_record_name(n)}
    except (gate_ok.RecordError, OSError) as exc:
        return None, 'no gate-OK records: %s' % ' '.join(str(exc).split())
    since = now - datetime.timedelta(days=DAYS)
    timed = {}
    for name in sorted(paths):
        try:
            with open(paths[name], encoding='utf-8') as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or gate_ok.mode_of(record) != 'full':
            continue
        ended = _ended(record)
        if ended is None or not since <= ended <= now:
            continue
        for step in record.get('steps') or ():
            if not isinstance(step, dict) or step.get('result') not in TIMED:
                continue
            secs = step.get('seconds')
            if isinstance(secs, bool) or not isinstance(secs, (int, float)) or secs <= 0:
                continue
            if isinstance(step.get('name'), str) and step['name']:
                timed.setdefault(step['name'], []).append((ended, float(secs)))
    return {name: [s for _e, s in sorted(got, key=lambda x: x[0], reverse=True)[:WINDOW]]
            for name, got in timed.items()}, None


def findings(root, now):
    """A `gate-time` finding per step whose median is over the bound; notes for too few samples,
    no records and a config that cannot be read."""
    conf, why = settings(root)
    if conf is None:
        return [], ['gate time NOT CHECKED: %s' % why]
    timed, why = samples(root, now)
    if timed is None:
        return [], ['gate time NOT CHECKED: %s' % why]
    if not timed:
        return [], ['gate time NOT CHECKED: no full gate record in the last %d days' % DAYS]
    out, notes = [], []
    bound = conf['bound_s']
    for name in sorted(timed):
        got = timed[name]
        if len(got) < MIN_SAMPLES:
            notes.append('gate time: %s has %d full gate sample(s) in %d days, under %d: not '
                         'judged' % (name, len(got), DAYS, MIN_SAMPLES))
            continue
        median = statistics.median(got)
        if median > bound:
            out.append(Finding(
                CHECK, name,
                'gate step %s takes a median %d s over its last %d full gate(s), over the bound '
                'of %d s' % (name, round(median), len(got), bound),
                'read the step\'s logs and make it faster, or raise "maint_lint".gate_time.bound_s '
                'in .kit.json'))
    return out, notes


def step_id(name):
    """A step name as id text: upper case, every run of other characters one hyphen."""
    return re.sub(r'[^A-Z0-9]+', '-', name.upper()).strip('-') or 'STEP'


def record_id(prefix, name, now):
    return '%s-GATE-TIME-%s-%s' % (prefix, step_id(name), now.strftime('%Y%m%d'))


def _live(snap, prefix, name):
    """The live record already filed for step `name`, or None: its ref, or its id (the stem
    and exactly 8 digits, so step `test` is not held by a `test-e2e` record)."""
    stem = re.compile(re.escape('%s-GATE-TIME-%s-' % (prefix, step_id(name))) + r'\d{8}')
    for rec in snap.readable:
        if rec.get('ref') == REF % name or stem.fullmatch(rec.stem):
            return rec.stem
    return None


def file(root, result, now=None, opener=None):
    """Files one record per `gate-time` row of `result` (kitlib.maintlint.run's dict) in the
    store of the checkout at `root`, and none for a step a live record already covers. The
    lines to print. `opener(root, session)`: kitlib.dailyfull.file.open_filing_store."""
    rows = [r for r in (result or {}).get('findings') or ()
            if isinstance(r, dict) and r.get('check') == CHECK]
    if not rows:
        return []
    from kitlib import maintlint
    now = now or datetime.datetime.now(datetime.timezone.utc)
    conf, why = settings(maintlint.code_checkout(root))
    if conf is None:
        return ['gate time: not filed: %s' % why]
    prefix = conf['prefix']
    if not prefix:
        return ['gate time: not filed (%d step(s) over the bound): no "maint_lint".gate_time.prefix'
                ' in .kit.json' % len(rows)]
    if opener is None:
        from kitlib.dailyfull.file import open_filing_store as opener
    lines = []
    try:
        ctx, why = opener(root, SLUG)
    except (Refusal, Failure, ConfigError, OSError) as exc:
        return ['gate time: not filed: %s' % ' '.join(str(exc).split())]
    if ctx is None:
        return ['gate time: not filed: %s' % why]
    for row in rows:
        name = row['target']
        rid = record_id(prefix, name, now)

        def add(s, name=name, rid=rid, text=row.get('text')):
            held = _live(s, prefix, name)
            if held is not None:
                return bstore.Change({}, '', ['%s is already filed for gate step %s' % (held, name)])
            return pmod.plan_add(s, ctx, rid, text, [bscope.NONE], None, REF % name,
                                 session=conf['session'], found_on=bscope.NONE,
                                 why_not_folded=NOT_FOLDED)
        try:
            change, _tip = ctx.store.write(add)
        except (Refusal, Failure, ConfigError, OSError) as exc:
            lines.append('gate time: not filed %s: %s' % (rid, ' '.join(str(exc).split())))
            continue
        lines.extend('gate time: %s' % line for line in change.lines)
    return lines
