"""The machine lock in one line, from the kit lock's own status (`tools/machine_lock.py status
--json`, kitlib.lock.api.snapshot; docs/lock-api.md, Snapshot), for preflight's `lock` line and
the board's `machine: lock` line.

The snapshot is read through the CLI, as any client reads it, never through lock internals: a
reading that fails or does not have the shape this module reads is `unknown (<why>)`, never
`free`. A queue or holds file the lock could not read is said so, never shown as none.
"""
import datetime
import json
import os
import subprocess
import sys

TIMEOUT = 30
ISO = '%Y-%m-%dT%H:%M:%SZ'


def read_snapshot(kit_root):
    """(snapshot dict, None) or (None, why), from `<kit_root>/tools/machine_lock.py status --json`."""
    tool = os.path.join(kit_root, 'tools', 'machine_lock.py')
    if not os.path.isfile(tool):
        return None, 'no tools/machine_lock.py in the kit'
    try:
        r = subprocess.run([sys.executable, '-B', tool, 'status', '--json'], capture_output=True,
                           text=True, encoding='utf-8', errors='replace', timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, 'machine_lock.py status did not finish: %s' % e
    if r.returncode != 0:
        tail = [l for l in (r.stderr or r.stdout or '').splitlines() if l.strip()]
        return None, 'machine_lock.py status exit %d%s' % (
            r.returncode, ': ' + tail[-1].strip() if tail else '')
    try:
        return json.loads(r.stdout), None
    except ValueError as e:
        return None, 'machine_lock.py status --json is not JSON: %s' % e


def _time(s):
    try:
        return datetime.datetime.strptime(s, ISO).replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def _local_start(s, now):
    """HH:MM local for a start today, YYYY-MM-DD HH:MM otherwise; None when not planned."""
    t = _time(s)
    if t is None:
        return None
    loc = t.astimezone()
    today = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone().date()
    return loc.strftime('%H:%M' if loc.date() == today else '%Y-%m-%d %H:%M')


def _ranges(nums):
    """[0, 1, 2, 5] -> '0-2,5'."""
    out, nums, i = [], sorted(nums), 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else '%d-%d' % (nums[i], nums[j]))
        i = j + 1
    return ','.join(out)


def _who(row):
    return '%s/%s' % (row.get('repo') or '?', row.get('slug') or '?')


def _shape_error(snap):
    if not isinstance(snap, dict):
        return 'the snapshot is not an object'
    slots = snap.get('slots')
    if not isinstance(slots, dict) or not all(
            isinstance(slots.get(p), list)
            and all(isinstance(s, dict) and isinstance(s.get('slot'), int) for s in slots[p])
            for p in ('cpu', 'gpu')):
        return 'no slots.cpu / slots.gpu lists'
    if not isinstance(snap.get('runs'), list) or not all(isinstance(r, dict) for r in snap['runs']):
        return 'no runs list'
    q = snap.get('queue')
    if not isinstance(q, dict) or not (q.get('entries') is None or isinstance(q['entries'], list)):
        return 'no queue object'
    if q.get('entries') and not all(isinstance(e, dict) for e in q['entries']):
        return 'a queue entry is not an object'
    if not isinstance(snap.get('holds') or [], list):
        return 'holds is not a list'
    return None


def _pool(snap, pool, runs, now):
    slots = snap['slots'][pool]
    by = {}
    for s in slots:
        if s.get('holder') is not None:
            by.setdefault(s['holder'], []).append(s['slot'])
    if not by:
        return '%s free (0/%d)' % (pool, len(slots))
    parts = []
    for rid, nums in sorted(by.items(), key=lambda kv: min(kv[1])):
        row = runs.get(rid) or {}
        start = _time(row.get('held_since') or row.get('since'))
        mins = '%d min' % ((now - start).total_seconds() // 60) if start and now else '? min'
        late = row.get('overrun') if isinstance(row.get('overrun'), dict) else None
        extra = (', OVERRUN +%d min' % (late.get('s', 0) // 60) if late else '') + (
            ', background' if row.get('state') == 'bg' or row.get('background') else '')
        parts.append('slot%s %s (%s, run %s, %s%s)' % (
            _ranges(nums), _who(row), row.get('kind') or '?', rid, mins, extra))
    return '%s %d/%d: %s' % (pool, sum(len(v) for v in by.values()), len(slots), ', '.join(parts))


def _need(needs):
    cores, gpus = needs.get('cpu') or 0, needs.get('gpu') or 0
    words = (['%s core%s' % (cores, '' if cores == 1 else 's')] if cores else []) + (
        ['%s gpu%s' % (gpus, '' if gpus == 1 else 's')] if gpus else [])
    return ' + '.join(words)


def _waiting(entries, now):
    shown = []
    for e in entries:
        needs = e.get('needs') if isinstance(e.get('needs'), dict) else {}
        need = _need(needs)
        start = _local_start(e.get('est_start'), now)
        shown.append('#%s %s (%s%s, %s%s%s)' % (
            e.get('position') or '?', _who(e), e.get('kind') or '?', ', ' + need if need else '',
            'starts %s' % start if start else 'start not planned',
            ', backfill' if e.get('backfill') else '', ', parked' if e.get('parked') else ''))
    return 'waiting %d: %s' % (len(entries), ', '.join(shown))


def _holds(holds):
    live = [h for h in holds if isinstance(h, dict) and h.get('live')]
    later = [h for h in holds if isinstance(h, dict) and not h.get('live')]
    out = ['hold %s until %s%s (%s)' % (h.get('scope') or '?', h.get('end_local') or '?',
                                        ': %s' % h['note'] if h.get('note') else '',
                                        h.get('owner') or '?') for h in live]
    if later:
        h = later[0]
        out.append('hold next: %s from %s (%s)' % (h.get('scope') or '?',
                                                  h.get('start_local') or '?', h.get('owner') or '?'))
    return out


def summary(snap):
    """`cpu ... | gpu ... | waiting ...[ | holds unreadable ...][ | hold ...]`, or `unknown (<why>)`."""
    why = _shape_error(snap)
    if why:
        return 'unknown (%s)' % why
    now = _time(snap.get('at'))
    runs = {r.get('id'): r for r in snap['runs']}
    parts = [_pool(snap, 'cpu', runs, now), _pool(snap, 'gpu', runs, now)]
    q = snap['queue']
    if q.get('entries') is None:
        parts.append('queue unreadable: %s' % (q.get('error') or 'no reason given'))
    else:
        parts.append(_waiting(q['entries'], now) if q['entries'] else 'waiting 0')
    if snap.get('holds_error'):
        parts.append('holds unreadable: %s' % snap['holds_error'])
    parts.extend(_holds(snap.get('holds') or []))
    return ' | '.join(parts)


def line_for(kit_root, reader=read_snapshot):
    """summary() of a fresh reading, or `unknown (<why>)` when it could not be read."""
    snap, why = reader(kit_root)
    return 'unknown (%s)' % why if snap is None else summary(snap)
