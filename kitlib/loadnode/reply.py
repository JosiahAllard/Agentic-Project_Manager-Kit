"""The load node's status reply: built on the node, checked and shown on the main machine.

The node prints one JSON line:

    {"protocol": 1, "ok": true, "kit_version": "0.4.0",
     "lock": {"cpu_slots": 4, "cpu_free": 3, "gpu_slots": 0, "gpu_free": 0,
              "running": 1, "waiting": 0, "holds": 0, "pending": false, "unreadable": []},
     "hardware": {"threads": 8, "cores": 4, "memory_mb": {"total": 15900, "free": 9100},
                  "gpus": [{"name": "...", "vram_mb": 8192}], "problems": 0},
     "verbs": ["status", "start", "follow", "fetch", "clean", "stop"], "jobs": 0}

or `{"protocol": 1, "ok": false, "error": "<why>"}`. build() makes it from the node's machine
lock snapshot (kitlib.lock.api.snapshot) as a projection: counts and hardware only, so no store
path, repository, slug or run note leaves the node. `verbs` (what the node serves) and `jobs` (the
`node.py run` jobs live on it) came with the run verbs: a node whose reply has no `verbs` serves
`status` only, so main can tell a node kit too old to take a run. Both are optional; the
protocol stays 1.

On the main machine the reply is untrusted (the node could be anyone's): parse() keeps only the
keys it knows, checks every type and range, and strips control and format characters from every
string and cuts it short before anything is shown. Nothing here prints or starts anything.
"""
import json
import os
import re
import unicodedata

PROTOCOL = 1
MAX_INT = 10 ** 9
MAX_GPUS = 16
UNREADABLE = ('queue', 'holds', 'kinds')     # lock parts the node could not read
COUNTS = ('cpu_slots', 'cpu_free', 'gpu_slots', 'gpu_free', 'running', 'waiting', 'holds')
MAX_VERBS = 16
VERB_RE = re.compile(r'[a-z][a-z-]{0,23}')


class ReplyError(Exception):
    """A reply that is not a status reply. The message never quotes the reply."""


def clean(text, cap):
    """`text` with every control, format, surrogate and private-use character removed (no
    terminal escape, no bidi override), cut to `cap` characters."""
    s = ''.join(c for c in str(text) if not unicodedata.category(c).startswith('C'))
    return s if len(s) <= cap else s[:cap - 3] + '...'


def kit_version(root):
    """The version in the kit folder `root`'s kit.json, or 'unknown'."""
    try:
        with open(os.path.join(root, 'kit.json'), encoding='utf-8') as f:
            v = json.load(f).get('version')
    except (OSError, ValueError, AttributeError):
        return 'unknown'
    return v if isinstance(v, str) else 'unknown'


# -- the node side -------------------------------------------------------------------------------

def _free(slots):
    return sum(1 for s in slots if s.get('holder') is None and s.get('hold') is None)


def _num(v):
    return v if type(v) is int and 0 <= v <= MAX_INT else None


def build(snapshot, kit_version, verbs=None, jobs=None):
    """The ok reply for the lock snapshot `snapshot` (kitlib.lock.api.snapshot's dict); `verbs`
    and `jobs` (the node's served words and its live jobs) are added when given."""
    hw = snapshot.get('hardware') or {}
    layout = snapshot.get('layout') or {}
    slots = snapshot.get('slots') or {}
    runs = snapshot.get('runs') or []
    cpu, gpu = slots.get('cpu') or [], slots.get('gpu') or []
    mem = hw.get('memory_mb') or {}
    unreadable = [k for k, err in (('queue', (snapshot.get('queue') or {}).get('error')),
                                   ('holds', snapshot.get('holds_error')),
                                   ('kinds', snapshot.get('kinds_error'))) if err]
    out = {
        'protocol': PROTOCOL, 'ok': True, 'kit_version': str(kit_version),
        'lock': {'cpu_slots': len(cpu), 'cpu_free': _free(cpu),
                 'gpu_slots': len(gpu), 'gpu_free': _free(gpu),
                 'running': sum(1 for r in runs if r.get('state') != 'waiting'),
                 'waiting': sum(1 for r in runs if r.get('state') == 'waiting'),
                 'holds': len(snapshot.get('holds') or []),
                 'pending': bool((layout.get('hardware_change') or {}).get('pending')),
                 'unreadable': unreadable},
        'hardware': {'threads': _num(hw.get('threads')), 'cores': len(hw.get('cores') or []),
                     'memory_mb': {'total': _num(mem.get('total')), 'free': _num(mem.get('free'))},
                     'gpus': [{'name': str(g.get('name') or ''), 'vram_mb': _num(g.get('vram_mb'))}
                              for g in (hw.get('gpus') or [])[:MAX_GPUS]],
                     'problems': len(hw.get('problems') or [])},
    }
    if verbs is not None:
        out['verbs'] = list(verbs)
    if jobs is not None:
        out['jobs'] = int(jobs)
    return out


def refusal(why):
    """The ok false reply."""
    return {'protocol': PROTOCOL, 'ok': False, 'error': why}


def dumps(reply):
    """The reply as the one line the node prints."""
    return json.dumps(reply, separators=(',', ':'), ensure_ascii=True)


# -- the main side -------------------------------------------------------------------------------

def _obj(v, what):
    if not isinstance(v, dict):
        raise ReplyError('%s is not an object' % what)
    return v


def _int(v, what, none_ok=False):
    if v is None and none_ok:
        return None
    if type(v) is not int or not 0 <= v <= MAX_INT:
        raise ReplyError('%s is not a whole number from 0 to %d' % (what, MAX_INT))
    return v


def _bool(v, what):
    if not isinstance(v, bool):
        raise ReplyError('%s is not true or false' % what)
    return v


def _str(v, what, cap):
    if not isinstance(v, str):
        raise ReplyError('%s is not a string' % what)
    return clean(v, cap)


def parse(text):
    """The checked reply in `text` (the node's stdout): a new dict of the known keys only.
    ReplyError when it is not one JSON object line of protocol 1 with every key well formed."""
    lines = [x for x in text.splitlines() if x.strip()]
    if len(lines) != 1:
        raise ReplyError('the reply is %d lines, not one' % len(lines))
    try:
        data = json.loads(lines[0])
    except ValueError:
        raise ReplyError('the reply is not JSON')
    data = _obj(data, 'the reply')
    if type(data.get('protocol')) is not int or data['protocol'] != PROTOCOL:
        raise ReplyError('the reply is not protocol %d' % PROTOCOL)
    if not _bool(data.get('ok'), 'ok'):
        return {'ok': False, 'error': _str(data.get('error'), 'error', 200)}
    lock = _obj(data.get('lock'), 'lock')
    hw = _obj(data.get('hardware'), 'hardware')
    mem = _obj(hw.get('memory_mb'), 'hardware.memory_mb')
    gpus = hw.get('gpus')
    if not isinstance(gpus, list) or len(gpus) > MAX_GPUS:
        raise ReplyError('hardware.gpus is not a list of at most %d' % MAX_GPUS)
    unreadable = lock.get('unreadable')
    if not isinstance(unreadable, list) or any(u not in UNREADABLE for u in unreadable):
        raise ReplyError('lock.unreadable is not a list of %s' % ', '.join(UNREADABLE))
    out_lock = {k: _int(lock.get(k), 'lock.' + k) for k in COUNTS}
    for pool in ('cpu', 'gpu'):
        if out_lock[pool + '_free'] > out_lock[pool + '_slots']:
            raise ReplyError('lock.%s_free is more than lock.%s_slots' % (pool, pool))
    out_lock.update(pending=_bool(lock.get('pending'), 'lock.pending'),
                    unreadable=sorted(set(unreadable)))
    verbs = data.get('verbs', [])
    if (not isinstance(verbs, list) or len(verbs) > MAX_VERBS
            or not all(isinstance(v, str) and VERB_RE.fullmatch(v) for v in verbs)):
        raise ReplyError('verbs is not a list of at most %d words' % MAX_VERBS)
    return {
        'ok': True,
        'kit_version': _str(data.get('kit_version'), 'kit_version', 40),
        'lock': out_lock,
        'hardware': {
            'threads': _int(hw.get('threads'), 'hardware.threads', True),
            'cores': _int(hw.get('cores'), 'hardware.cores'),
            'memory_mb': {'total': _int(mem.get('total'), 'hardware.memory_mb.total', True),
                          'free': _int(mem.get('free'), 'hardware.memory_mb.free', True)},
            'gpus': [{'name': _str(_obj(g, 'a gpu').get('name'), 'a gpu name', 80),
                      'vram_mb': _int(g.get('vram_mb'), 'a gpu\'s vram_mb', True)} for g in gpus],
            'problems': _int(hw.get('problems'), 'hardware.problems'),
        },
        'verbs': sorted(set(verbs)),
        'jobs': _int(data.get('jobs'), 'jobs', True),
    }


def _gb(mb):
    return '?' if mb is None else '%.1f' % (mb / 1024.0)


def status_lines(results, nodes, kit_version=None):
    """The status table: one line per node, in registry order. `results` are
    kitlib.loadnode.remote.Result, `nodes` the registry's Node list, `kit_version` this
    machine's kit version (a node on another is marked)."""
    caps = {n.name: n for n in nodes}
    rows = [('NODE', 'STATE', 'CPU FREE', 'GPU FREE', 'MEMORY FREE', 'RUN+WAIT', 'REGISTRY', 'KIT')]
    notes = []
    for r in results:
        n = caps.get(r.name)
        cap = '%d slot%s, %s GB' % (n.slots, '' if n.slots == 1 else 's', _gb(n.memory_mb)) if n else '?'
        if r.reply is None:
            rows.append((r.name, r.state, '-', '-', '-', '-', cap, '-'))
            notes.append('%s: %s%s' % (r.name, r.state, ': ' + r.detail if r.detail else ''))
            continue
        lk, hw = r.reply['lock'], r.reply['hardware']
        mem = hw['memory_mb']
        kit = r.reply['kit_version']
        if kit_version and kit != kit_version:
            kit += ' (here %s)' % kit_version
        rows.append((r.name, r.state, '%d/%d' % (lk['cpu_free'], lk['cpu_slots']),
                     '%d/%d' % (lk['gpu_free'], lk['gpu_slots']),
                     '%s/%s GB' % (_gb(mem['free']), _gb(mem['total'])),
                     '%d+%d' % (lk['running'], lk['waiting']), cap, kit))
        if lk['pending']:
            notes.append('%s: its machine lock has a hardware change waiting to be adopted' % r.name)
        if lk['unreadable']:
            notes.append('%s: its machine lock could not read its %s' % (r.name, ', '.join(lk['unreadable'])))
        if hw['problems']:
            notes.append('%s: its hardware reading had %d problem(s)' % (r.name, hw['problems']))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    out = ['  '.join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows]
    return out + notes
