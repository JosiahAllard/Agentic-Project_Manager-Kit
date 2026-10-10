"""Gate reuse: a landing's merged-tree gate counts a step as passed, without running it, when the
branch gate passed that step on exactly the tree the landing merged (docs/gate.md "Gate reuse";
ported from one repository's tools/gate_reuse.py).

A step opts in with `"reuse": true` (or a group name: the steps of one group are reused together
or not at all). A check step never does. Fail-closed: any doubt, any error, means the step runs.

The landing reuses nothing unless every one of these holds:
  - not --fresh and not --full, and the landing merges one branch (not a batch)
  - no `land` result record names this tip from before the landing started (a landing of this
    tip got past its locked checks and did not land), and no `gate` result record names it
    with a verdict other than pass (a later red or unfinished run of the same commit)
  - the branch's own `gate` result record names this tip, verdict pass, and the gate-OK record
  - that gate-OK record verifies (kitlib.gate.runner.verify_record) with config_rev == the tip,
    its gate_config is the hash of the landing's gate config (the one --expect-main holds), its
    base is --expect-main, its tree is the merge's tree, its kit identity is the running kit's
    and both are clean, and it ended at most "landing".reuse_max_age_h hours ago
  - the merge changes no `.kit.json`, no config section file, no hook module and no path
    matching "landing".reuse_never; the main checkout is clean
Then each step is reused when it asks for reuse, the record has it with result pass, and its
argv in the merged gate is its branch-gate argv (a step's merged_args, or the lane's, make it
run); a group is reused only when every selected member is.
"""
import collections
import datetime
import os

from kitlib.config import schema
from kitlib.config.load import CONFIG_NAME
from kitlib.gate import result, runner
from kitlib.gate import steps as S
from kitlib.git import refs
from kitlib.records import gate_ok

Plan = collections.namedtuple('Plan', 'reused lines')   # reused: {step name: why}; lines to say
NOTHING = Plan({}, [])
SKEW_S = 60         # a record that ended up to this far in the future (clock skew) still counts
PREFIX = 'gate: reuse: '


def prior_records(root, tips):
    """The result records that name any of `tips`, read before a landing writes its own: a list
    of dicts, or None when the folder cannot be read (then nothing is reused)."""
    try:
        folder = result.result_dir(root)
        names = os.listdir(folder) if os.path.isdir(folder) else []
    except Exception:       # noqa: BLE001 - cannot tell: reuse nothing
        return None
    out = []
    for n in sorted(names):
        if not n.endswith('.json') or n.startswith('.'):
            continue
        rec, why = result.read_path(os.path.join(folder, n))
        if rec is None:
            return None     # a record nobody can read might be the red one
        if rec.get('tip') in tips:
            out.append(rec)
    return out


def _group(spec):
    r = spec.get('reuse')
    if r is True:
        return '=' + spec['name']           # its own group ('=' never starts a step name)
    return r if isinstance(r, str) and r else None


def _age_why(rec, max_h, now):
    if max_h <= 0:
        return '"landing".reuse_max_age_h is 0'
    try:
        ended = datetime.datetime.strptime(rec['ended_utc'], '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc)
    except (KeyError, TypeError, ValueError):
        return 'the gate-OK record names no readable ended_utc'
    age = (now - ended).total_seconds()
    if age < -SKEW_S:
        return 'the gate-OK record ended in the future (%s)' % rec['ended_utc']
    if age > max_h * 3600:
        return 'the branch gate passed %.1f h ago, more than "landing".reuse_max_age_h (%d)' % (
            age / 3600.0, max_h)
    return None


def landing_why(root, cfg, branch, tip, base, merge, prior, fresh, full, batch, now):
    """`(record, None)` when the landing may reuse steps of the branch gate's record, else
    `(None, why not)`."""
    if fresh:
        return None, '--fresh'
    if full:
        return None, '--full'
    if batch != 1:
        return None, 'a batch of %d branches is gated whole' % batch
    if prior is None:
        return None, 'the result records cannot all be read'
    for r in prior:
        if r.get('kind') == 'land':
            return None, ('%s at %s was landed before (%s) and did not land: its tree is gated '
                          'again' % (r.get('branch'), tip[:10], r.get('verdict')))
        if r.get('kind') == 'gate' and r.get('verdict') != 'pass':
            return None, ('a gate of %s on %s ended %s' % (tip[:10], r.get('branch'),
                                                            r.get('verdict')))
    try:
        record_path = gate_ok.record_path(root, tip)
    except gate_ok.RecordError as e:
        return None, str(e)
    own = [r for r in prior if r.get('kind') == 'gate' and r.get('branch') == branch]
    if not own or own[0].get('verdict') != 'pass' or not own[0].get('gate_ok_record') or \
            os.path.normcase(os.path.abspath(own[0]['gate_ok_record'])) != \
            os.path.normcase(os.path.abspath(record_path)):
        return None, 'no passing branch gate of %s at %s wrote its gate-OK record' % (
            branch, tip[:10])
    rec, why = runner.verify_record(root, tip)
    if rec is None:
        return None, 'the gate-OK record: %s' % why
    if rec.get('config_rev') != tip:
        return None, 'the gate-OK record of %s was not written by its branch gate' % tip[:10]
    if rec.get('gate_config') != gate_ok.config_hash(runner.gate_config(cfg)):
        return None, 'the branch gate ran another gate config than --expect-main holds'
    if rec.get('base') != base:
        return None, 'the branch gate compared against %s, not --expect-main %s' % (
            str(rec.get('base'))[:10], base[:10])
    tree, why = refs.tree_of(merge, root)
    if not tree or rec.get('tree') != tree:
        return None, 'the merge tree is not the tree the branch gate passed'
    kit = runner.kit_identity()
    if not refs.is_sha(kit.get('commit') or '') or kit.get('clean') is not True:
        return None, 'the kit checkout is not a clean commit'
    if rec.get('kit') != kit:
        return None, 'the branch gate ran under another kit (%s) than this landing (%s)' % (
            rec.get('kit'), kit)
    why = _age_why(rec, cfg.section('landing')['reuse_max_age_h'], now)
    if why:
        return None, why
    changed, why = runner.changed_between(root, base, merge)
    if changed is None:
        return None, 'git cannot list the merged paths: %s' % why
    never = [CONFIG_NAME] + [f for f in (cfg.section_file(n) for n in schema.SECTIONS) if f]
    hook = cfg.hook()
    if hook:
        never.append('%s/%s.py' % (cfg.tools_dir.replace('\\', '/').strip('/'), hook['module']))
    never = [n.lower() for n in never]
    globs = cfg.section('landing')['reuse_never']
    hit = [p for p in changed if p.lower() in never or S._match(p, globs)]
    if hit:
        return None, 'the merge changes %s' % ', '.join(hit[:5])
    entries, why = refs.status(root)
    if entries != []:
        return None, 'the main checkout is not clean%s' % (': %s' % why if why else '')
    return rec, None


def plan(root, cfg, branch, tip, base, merge, prior, chosen, fresh=False, full=False, batch=1,
         now=None):
    """The Plan for the merged gate's `chosen` steps."""
    gate = cfg.section('gate')
    specs = {s['name']: s for s in gate.get('steps') or []}
    asking = [s for s in chosen if s.name in specs and _group(specs[s.name]) and s.cmd]
    if not asking:
        return NOTHING
    now = now or datetime.datetime.now(datetime.timezone.utc)
    rec, why = landing_why(root, cfg, branch, tip, base, merge, prior, fresh, full, batch, now)
    if rec is None:
        return Plan({}, [PREFIX + 'none: %s' % why])
    passed = {s.get('name') for s in rec.get('steps') or [] if s.get('result') == 'pass'}
    plain = {s.name: s for s in S.build(gate, root, base, cfg.tools_dir)}
    ok, lines = {}, []
    for s in asking:
        b = plain.get(s.name)
        if s.name not in passed:
            lines.append(PREFIX + '%s runs: the branch gate did not pass it' % s.name)
        elif b is None or b.cmd != s.cmd or b.cwd != s.cwd:
            lines.append(PREFIX + '%s runs: its merged-gate command differs (merged_args)' % s.name)
        else:
            ok[s.name] = 'passed in the branch gate of %s (%s)' % (tip[:10], rec.get('ended_utc'))
    groups = collections.defaultdict(list)
    for s in asking:
        groups[_group(specs[s.name])].append(s.name)
    for g, names in groups.items():
        missing = [n for n in names if n not in ok]
        if missing and len(names) > len(missing):
            for n in names:
                if n in ok:
                    del ok[n]
                    lines.append(PREFIX + '%s runs: %s of its group %s runs' % (
                        n, ', '.join(missing), g))
    return Plan(ok, lines)


def planner(root, cfg, branch, tip, base, merge, prior, fresh=False, full=False, batch=1,
            now=None):
    """`reuse(chosen) -> Plan` for kitlib.gate.runner.merged_gate. Anything that raises means
    nothing is reused (the reason is said)."""
    def reuse(chosen):
        try:
            return plan(root, cfg, branch, tip, base, merge, prior, chosen, fresh=fresh,
                        full=full, batch=batch, now=now)
        except Exception as e:      # noqa: BLE001 - fail closed: every step runs
            return Plan({}, [PREFIX + 'none: %s: %s' % (type(e).__name__, e)])
    return reuse
