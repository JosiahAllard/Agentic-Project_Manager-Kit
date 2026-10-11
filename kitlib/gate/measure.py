"""A proposed kinds table from the machine lock's run history (`gate.py --measure`).

Sizes change only by a PM-reviewed commit: propose() returns a new table and the entry writes it
to a file the PM reviews, never over the live kitlib/lock/kinds.json.

Per kind, over its finished runs (history `how` "done") in the last `days`:
  fewer than MIN_RUNS (the lock's history_min_runs) runs: the entry is kept as it is
  cpu samples  the lock measures a run's job object only, so a finished run is a cpu sample
            only when its line shows work in the job (skip_reason): it is not `partial`, it
            held MIN_HELD_S or more, it has `cpu_s` and `peak_cpu_cores`, and its cpu_s is at
            least MIN_BUSY_THREADS x held_s. Fewer than MIN_RUNS cpu samples: the entry is
            kept as it is, warn_min too, and the note counts the skipped runs by reason. A
            kind that really idles in its job is skipped the same way and keeps its size. Not
            caught: a wrapper that is itself that busy while the work runs outside the job.
  warn_min  ceil(p90 held seconds * WARN_MARGIN / 60), at least 1
  cpu       over the cpu samples. With agent_cap (the layout's; gate.py --measure gives it):
            a whole number of cores 1..agent_cap. Runs are pinned to the cores they booked, so a
            run cannot show more than its booking: when the p90 of peak / booked reaches
            SATURATED the kind is proposed agent_cap (the next round measures it unpinned by its
            old size);
            else ceil(p90 peak busy cpu slots), at least 1. A run's booking is its history
            `cores` (the slots it held), else its need.cpu, else the entry's size now.
            Without agent_cap (the rule before whole-core sizes): heavy when the p90 peak reaches
            SATURATED of the layout's medium_cores, else medium.
  gpu, share  kept: history does not measure gpu use
With agent_cap every kind in the proposal is a whole number: a kept kind (too few runs) is
written as the size it books on this layout now, and its note says so. Each measured entry gains
a `_measured` note: runs, median and p90 held seconds, cpu_samples (and cpu_skipped, a count
per reason, when any run was skipped), p90 and max peak cpu slots of the cpu samples, max peak
memory (MB), and the layout figures used. p90 is the nearest-rank percentile.
"""
import copy
import math

WARN_MARGIN = 1.5
SATURATED = 0.9
MIN_HELD_S = 10             # two of the lock's 5 s sample windows (usage_sample_s); under: no peak
MIN_BUSY_THREADS = 0.1      # cpu_s / held_s, the job's mean busy cpu threads, of a run seen working
SKIPPED = (('partial', 'partial (docker exec: use partly outside the job)'),
           ('short', 'held under %d s (no peak window)' % MIN_HELD_S),
           ('no_figures', 'without cpu figures'),
           ('idle', "under %s cpu thread busy (work outside the run's job, or idle)"
            % MIN_BUSY_THREADS))


def percentile(values, q):
    """Nearest-rank percentile of a non-empty list."""
    xs = sorted(values)
    return xs[max(0, min(len(xs) - 1, int(math.ceil(q * len(xs))) - 1))]


def _num(d, key):
    v = d.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def skip_reason(d):
    """Why finished run `d` is not a cpu sample (a SKIPPED key, the first that applies), or
    None when its line shows work in the run's job (see the module note)."""
    if d.get('partial'):
        return 'partial'
    if _num(d, 'held_s') < MIN_HELD_S:
        return 'short'
    if _num(d, 'cpu_s') is None or _num(d, 'peak_cpu_cores') is None:
        return 'no_figures'
    if _num(d, 'cpu_s') < MIN_BUSY_THREADS * _num(d, 'held_s'):
        return 'idle'
    return None


def _skipped_text(skipped):
    return ', '.join('%d %s' % (skipped[k], text) for k, text in SKIPPED if skipped.get(k))


def size_now(cpu, medium_cores, agent_cap):
    """The cores an entry's cpu books outside an owner whole hold: an alias's size, or a whole
    number clamped to 1..agent_cap (kitlib.lock.layout.kind_cores, from the layout figures)."""
    if cpu == 'medium':
        return medium_cores
    if cpu == 'heavy':
        return agent_cap
    if isinstance(cpu, int) and not isinstance(cpu, bool):
        return max(1, min(cpu, agent_cap))
    return agent_cap


def _booked(d, entry_size):
    cores = d.get('cores')
    if isinstance(cores, list) and cores:
        return len(cores)
    need = (d.get('need') or {}).get('cpu') if isinstance(d.get('need'), dict) else None
    if isinstance(need, int) and not isinstance(need, bool) and need >= 1:
        return need
    return entry_size


def _cores(peaks, booked, agent_cap):
    """The whole number of cores a kind's measured runs want (see the module note)."""
    fill = [p / b for p, b in zip(peaks, booked) if b]
    if fill and percentile(fill, 0.9) >= SATURATED:
        return agent_cap
    return max(1, min(agent_cap, int(math.ceil(percentile(peaks, 0.9)))))


def propose(table, history, medium_cores, min_runs=3, agent_cap=None):
    """`(new table, notes)`: `table` is the kinds file's "kinds" object; `history` the lock's
    history lines; notes one line per kind saying what changed or why it was kept. agent_cap
    (the layout's) proposes a whole number of cores per kind; without it the old
    medium/heavy rule."""
    out, notes = copy.deepcopy(table), []
    for kind in sorted(table):
        entry = out[kind]
        if not isinstance(entry, dict):
            continue                # a note in the table, not a kind
        runs = [d for d in history if d.get('kind') == kind and d.get('how') == 'done'
                and _num(d, 'held_s') is not None]
        samples, skipped = [], {}
        for d in runs:
            why = skip_reason(d)
            if why is None:
                samples.append(d)
            else:
                skipped[why] = skipped.get(why, 0) + 1
        if len(samples) < min_runs:
            note = '%s: kept (%d finished run(s); %d needed)' % (kind, len(runs), min_runs)
            if len(runs) >= min_runs:
                note = ('%s: kept (%d cpu sample(s) of %d finished run(s); %d needed; skipped: %s)'
                        % (kind, len(samples), len(runs), min_runs, _skipped_text(skipped)))
            if agent_cap is not None:
                size = size_now(entry.get('cpu'), medium_cores, agent_cap)
                if size != entry.get('cpu'):
                    note += '; cpu %s -> %d (its size on this layout)' % (entry.get('cpu'), size)
                entry['cpu'] = size
            notes.append(note)
            continue
        held = [_num(d, 'held_s') for d in runs]
        peaks = [_num(d, 'peak_cpu_cores') for d in samples]
        mems = [m for m in (_num(d, 'peak_mem_mb') for d in runs) if m is not None]
        warn = max(1, int(math.ceil(percentile(held, 0.9) * WARN_MARGIN / 60.0)))
        cpu = entry.get('cpu')
        if agent_cap is not None:
            size = size_now(cpu, medium_cores, agent_cap)
            booked = [_booked(d, size) for d in samples]
            cpu = _cores(peaks, booked, agent_cap) if peaks else size
        elif peaks:
            cpu = 'heavy' if percentile(peaks, 0.9) >= SATURATED * medium_cores else 'medium'
        changes = []
        if warn != entry.get('warn_min'):
            changes.append('warn_min %s -> %s' % (entry.get('warn_min'), warn))
        if cpu != entry.get('cpu'):
            changes.append('cpu %s -> %s' % (entry.get('cpu'), cpu))
        entry['warn_min'], entry['cpu'] = warn, cpu
        entry['_measured'] = {
            'runs': len(runs), 'held_s_median': round(percentile(held, 0.5), 1),
            'held_s_p90': round(percentile(held, 0.9), 1), 'cpu_samples': len(samples),
            'peak_cpu_slots_p90': round(percentile(peaks, 0.9), 2) if peaks else None,
            'peak_cpu_slots_max': round(max(peaks), 2) if peaks else None,
            'peak_mem_mb_max': int(max(mems)) if mems else None,
            'medium_cores': medium_cores}
        if agent_cap is not None:
            entry['_measured']['agent_cap'] = agent_cap
        count = '%d runs' % len(runs)
        if skipped:
            entry['_measured']['cpu_skipped'] = skipped
            count += '; cpu from %d, skipped: %s' % (len(samples), _skipped_text(skipped))
        notes.append('%s: %s (%s)' % (kind, '; '.join(changes) or 'unchanged', count))
    return out, notes


def document(kinds, note, aliases=None):
    """The kinds file object for a proposal: the live file's shape with its note replaced; the
    live file's `aliases` object, when it has one, is carried over as it is, so committing a
    proposal never drops an alias."""
    doc = {'_note': note, 'kinds': kinds}
    if aliases:
        doc['aliases'] = copy.deepcopy(aliases)
    return doc
