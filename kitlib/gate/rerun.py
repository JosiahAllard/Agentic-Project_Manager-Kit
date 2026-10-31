"""The targeted rerun of a red `gate.py --full` (docs/gate.md "Targeted rerun").

A red `gate.py --full` of a clean tree writes a red run (record_red, called by
kitlib.gate.runner): the commit, its tree and author, the gate config's hash, each step's
status and, for each FAIL step, the failing test ids parsed from its log (parse_log) with the
mode each failed in, or why the step is uncovered. `gate.py --rerun` (rerun_gate) then runs,
under one machine-lock slot booked as a gate run of the branch (so it counts toward the branch
gate budget, kitlib.gate.budget), each failing test class with `python -m unittest -v <class>`
at the tip, each paired with the same class at the merge base in a throwaway tree (the
control), and every step the red run did not reach (NOT RUN) whole. It writes a rerun record
and says FLAKE or REAL.

FLAKE: every failing id passed at the tip, every NOT RUN step passed, warned or skipped, no step
was uncovered, and no id that failed on the tip has failed twice on the branch (branch_twice).
Anything else is REAL. The
control is information only: a red control never turns a REAL into a FLAKE; it is said and
recorded as `also red at base`. A FLAKE prints the `backlog.py add` line the builder reports;
the coordinator decides on filing it.

A landing (kitlib.gate.land) takes a tip with no full gate-OK record when verify_flake finds
its latest red run and a FLAKE rerun of exactly that run, and refuses any tip that has a red
run of its own and an id that failed on it and failed twice on the branch (twice_refusal),
whatever other record it has. A tip with no red run of its own is not counted: a green
`gate.py --full` of a fix lands. Every missing, unreadable or mismatched record refuses (fail
closed).

Uncovered (the rerun refuses; only a passing `gate.py --full` lands the tip): a FAIL step whose
reasons are more than its exit code and a missing pass pattern; one whose log is neither
kit_tests' report nor unittest's (a gradle or dotnet log, say); a kit_tests unit that is not
FAILED (TIMEOUT, a runner
failure); a test that could not be loaded (`unittest.loader._FailedTest`); FAIL/ERROR lines
that do not add up to the FAILED summary; an unexpected success; an interrupted or refused
kit_tests run; a step whose directory is outside the tree.

Records: `<dev_drive>/pm-kit/kit-gate-rerun/<repo key>/<commit>/`, one file per run, written
once (red-<ns>-<token>.json, rerun-<ns>-<token>.json), each carrying the sha256 of its own
canonical JSON. A commit's history is every file there; it is never rewritten. A branch's
history is the history of the tip and of every commit `git rev-list <tip> ^<merge base>` names.
"""
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time

from kitlib import gate as G
from kitlib import kitroot
import kitlib.config as kconfig
from kitlib.config import policy
from kitlib.config.types import ConfigError
from kitlib.gate import budget
from kitlib.gate import detect
from kitlib.gate import gatecap
from kitlib.gate import gitstatus
from kitlib.gate import result
from kitlib.gate import runner
from kitlib.gate import stash
from kitlib.gate import steps as S
from kitlib.git import cmd, refs, repo
from kitlib.lock import run as lockrun
from kitlib.lock.store import LockError, Store
from kitlib.proc.job import JobError
from kitlib.records import gate_ok

DIR_NAME = 'kit-gate-rerun'
SCHEMA = 1
RED, RERUN = 'red', 'rerun'
FLAKE, REAL, VOID = 'FLAKE', 'REAL', 'VOID'
PASSING = ('PASS', 'WARN', 'SKIP', 'REUSED')
FIXTURES = ('setUpClass', 'tearDownClass', 'setUpModule', 'tearDownModule')
FILE_NAME = re.compile(r'^(red|rerun)-(\d+)-([0-9a-f]{8})\.json$')

SECTION = re.compile(r'^==== (\S+) \((\d+) tests?, [\d.]+s, (.+?)\) ====\s*$')
TIER = re.compile(r'^kit_tests: .*: tier .+ \(KIT_FAKE_GIT=([01])\)\s*$')
KT_LINE = re.compile(r'^kit_tests(?: \[[^\]]+\])?: (.*)$')
FAILED_LIST = re.compile(
    r'^kit_tests(?: \[[^\]]+\])?: (?:FAILED|INTERRUPTED)[^;]*;.*?failed: (.+)$')
ENTRY = re.compile(r'(\S+) \(([^)]+)\)')
TEST_LINE = re.compile(r'^(FAIL|ERROR): (\S+) \(([^)\s]+)\)')
SUMMARY = re.compile(r'^FAILED \(([^)]*)\)\s*$')
RAN = re.compile(r'^Ran (\d+) tests? in ')
OK_LINE = re.compile(r'^OK(?: \(.*\))?$')
PROGRESS = re.compile(r'^(\w+) \(([\w.]+)\)(.*)$')
COVERED_REASON = re.compile(r'^(exit \S+|missing pass pattern .*)$')
ABSENT = re.compile(r'ModuleNotFoundError|ImportError|AttributeError: module .* has no attribute')


class RerunError(Exception):
    """A rerun record could not be found, read or written."""


# ---------------------------------------------------------------- the records

def record_dir(root):
    """`<dev_drive>/pm-kit/kit-gate-rerun/<repo key>` of the repository `root` is in."""
    common, why = repo.common_dir(root)
    if not common:
        raise RerunError('no git common dir for %s: %s' % (root, why))
    try:
        base = result.kit_folder(DIR_NAME, 'gate rerun folder')
    except result.ResultError as e:
        raise RerunError(str(e))
    return os.path.join(base, result.repo_key(common))


def commit_dir(root, commit):
    if not refs.is_sha(commit or ''):
        raise RerunError('%r is not a full commit hash' % (commit,))
    return os.path.join(record_dir(root), commit)


def digest(rec):
    """sha256 of the canonical JSON of `rec` without its `hash` key."""
    body = {k: v for k, v in rec.items() if k != 'hash'}
    return hashlib.sha256(gate_ok.canonical_json(body).encode('utf-8')).hexdigest()


def _write(root, rec):
    folder = commit_dir(root, rec['commit'])
    rec = dict(rec, schema=SCHEMA, ns=time.time_ns())
    rec['hash'] = digest(rec)
    path = os.path.join(folder, '%s-%d-%s.json' % (rec['kind'], rec['ns'], secrets.token_hex(4)))
    try:
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, '.%s.tmp' % secrets.token_hex(6))
        try:
            with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(rec, sort_keys=True, indent=1) + '\n')
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    except OSError as e:
        raise RerunError('cannot write %s: %s' % (path, e))
    return path, rec


def load(root, commit):
    """`(reds, reruns)` recorded for `commit`, each oldest first; ([], []) when none. RerunError
    when the folder cannot be found, or any file in it is not a whole, unaltered record of
    `commit`."""
    folder = commit_dir(root, commit)
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return [], []
    except OSError as e:
        raise RerunError('cannot list %s: %s' % (folder, e))
    reds, reruns = [], []
    for name in names:
        if name.startswith('.') and name.endswith('.tmp'):
            continue                # a write in flight, or one that died before its replace
        m = FILE_NAME.match(name)
        path = os.path.join(folder, name)
        if not m:
            raise RerunError('%s is not a rerun record' % path)
        try:
            with open(path, encoding='utf-8') as f:
                rec = json.load(f)
        except (OSError, ValueError) as e:
            raise RerunError('%s cannot be read: %s' % (path, e))
        if not isinstance(rec, dict) or rec.get('schema') != SCHEMA:
            raise RerunError('%s is not a schema %d record' % (path, SCHEMA))
        if rec.get('hash') != digest(rec):
            raise RerunError('%s does not match its hash (changed after it was written)' % path)
        if rec.get('commit') != commit or rec.get('kind') != m.group(1) or \
                rec.get('ns') != int(m.group(2)):
            raise RerunError('%s does not match its name or commit' % path)
        (reds if m.group(1) == RED else reruns).append(rec)
    key = lambda r: r['ns']         # noqa: E731
    return sorted(reds, key=key), sorted(reruns, key=key)


def failure_counts(reds, reruns):
    """{test id: how many runs of the commit it failed in}: each red run's parsed failures, and
    each rerun's tip runs of an id that did not pass."""
    counts = {}
    for red in reds:
        for tid in {f['id'] for s in red.get('steps') or [] for f in s.get('failures') or []}:
            counts[tid] = counts.get(tid, 0) + 1
    for rr in reruns:
        for tid in {t for c in rr.get('classes') or [] for t, st in (c.get('ids') or {}).items()
                    if st != 'ok'}:
            counts[tid] = counts.get(tid, 0) + 1
    return counts


def twice(reds, reruns):
    return sorted(t for t, n in failure_counts(reds, reruns).items() if n >= 2)


def _twice_text(ids):
    return ('%s failed on this tip and failed twice on its branch: a real failure, which never '
            'lands; fix it (skill flaky-test-triage) and gate the new tip' % ', '.join(ids[:5]))


def branch_commits(root, tip, main):
    """`(commits, None)`: `tip` and every other commit of its branch, `git rev-list <tip>
    ^<merge base of tip and main>`; or `(None, why)`."""
    mb, why = refs.merge_base(tip, main, root)
    if not mb:
        return None, 'git cannot find the merge base of %s and %s: %s' % (
            tip[:10], (main or '')[:10], why or 'none')
    out, why = cmd.run_text(['rev-list', tip, '^' + mb, '--'], root)
    if out is None:
        return None, 'git cannot list the commits of the branch: %s' % why
    commits = out.split()
    if not all(refs.is_sha(c) for c in commits):
        return None, 'git rev-list printed %r, not commits' % out[:120]
    return [tip] + [c for c in commits if c != tip], None


def branch_twice(root, tip, main, reds, reruns):
    """`(ids, None)`: the ids that failed on `tip` (its own `reds` and `reruns`) and failed
    at least twice over the branch (branch_commits; failure_counts summed over every commit's
    records), sorted; or `(None, why)` when the branch or any commit's records cannot be read,
    whatever the tip's own count (fail closed: every record is read before any answer)."""
    commits, why = branch_commits(root, tip, main)
    if commits is None:
        return None, why
    others = []
    for c in commits[1:]:
        try:
            others.append(failure_counts(*load(root, c)))
        except RerunError as e:
            return None, 'the rerun records of the branch commit %s cannot be read: %s' % (
                c[:10], e)
    total = failure_counts(reds, reruns)
    for counts in others:
        for tid, n in counts.items():
            if tid in total:
                total[tid] += n
    return sorted(t for t, n in total.items() if n >= 2), None


# ---------------------------------------------------------------- parsing a step's log

def _test_id(method, paren):
    """`(id, class)` of a FAIL/ERROR line's method and parenthesis: the 3.11+ form
    `test_x (pkg.mod.Class.test_x)`, the older `test_x (pkg.mod.Class)`, and a fixture
    `setUpClass (pkg.mod.Class)` (its class, or module, is rerun whole)."""
    if method in FIXTURES:
        return '%s.%s' % (paren, method), paren
    if paren.endswith('.' + method):
        return paren, paren[:-(len(method) + 1)]
    return '%s.%s' % (paren, method), paren


def _counts(summary):
    out = {}
    for part in summary.split(','):
        k, _, v = part.strip().partition('=')
        try:
            out[k.strip()] = int(v)
        except ValueError:
            pass
    return out


def _parse_block(lines, env, where):
    """`(failures, uncovered)` of one unittest run's (or several's) text."""
    failures, uncovered, seen = [], [], set()
    for line in lines:
        m = TEST_LINE.match(line)
        if not m:
            continue
        method, paren = m.group(2), m.group(3)
        if paren.startswith('unittest.') or '_FailedTest' in paren:
            uncovered.append('%s: a test could not be loaded (%s)' % (where, paren))
            continue
        tid, cls = _test_id(method, paren)
        seen.add(tid)
        failures.append({'id': tid, 'cls': cls, 'env': dict(env)})
    lines_n = sum(1 for line in lines if TEST_LINE.match(line))
    sums = [_counts(m.group(1)) for m in (SUMMARY.match(line) for line in lines) if m]
    if not sums:
        uncovered.append('%s: no unittest FAILED summary' % where)
    else:
        total = sum(c.get('failures', 0) + c.get('errors', 0) for c in sums)
        if sum(c.get('unexpected successes', 0) for c in sums):
            uncovered.append('%s: an unexpected success' % where)
        if total < 1 or total != lines_n:
            uncovered.append('%s: %d FAIL/ERROR line(s) against %d in the FAILED summary'
                             % (where, lines_n, total))
    unique = []
    for f in failures:
        if f['id'] not in [u['id'] for u in unique]:
            unique.append(f)
    return unique, uncovered


def _parse_kit_tests(lines):
    failures, uncovered = [], []
    env, sections, listed, cur = {}, [], [], None
    for line in lines:
        s = SECTION.match(line)
        if s:
            cur = {'unit': s.group(1), 'status': s.group(3), 'env': dict(env), 'body': []}
            sections.append(cur)
            continue
        k = KT_LINE.match(line)
        if k:
            cur = None
            t = TIER.match(line)
            if t:
                env = {'KIT_FAKE_GIT': t.group(1)}
            text = k.group(1)
            if text.startswith('INTERRUPTED') or 'REFUSED' in text or 'refused' in text:
                uncovered.append('kit_tests: %s' % text.strip()[:200])
            f = FAILED_LIST.match(line)
            if f:
                listed.append((f.group(1), dict(env)))
            continue
        if cur is not None:
            cur['body'].append(line)
    for sec in sections:
        where = 'kit_tests unit %s' % sec['unit']
        if sec['status'] != 'FAILED':
            uncovered.append('%s: %s' % (where, sec['status']))
            continue
        got, why = _parse_block(sec['body'], sec['env'], where)
        failures += got
        uncovered += why
    have = {(sec['unit'], json.dumps(sec['env'], sort_keys=True)) for sec in sections}
    for text, lenv in listed:
        for unit, status in ENTRY.findall(text):
            if status != 'FAILED':
                uncovered.append('kit_tests unit %s: %s' % (unit, status))
            elif (unit, json.dumps(lenv, sort_keys=True)) not in have:
                uncovered.append('kit_tests unit %s: listed as failed, with no report' % unit)
    if not sections and not listed:
        uncovered.append('kit_tests: no failing unit is listed')
    return failures, uncovered


def parse_log(text):
    """`(failures, uncovered)` of a FAIL step's log: failures [{id, cls, env}] (env: the
    variables the pass that failed it set, `{"KIT_FAKE_GIT": "0"|"1"}` after a kit_tests tier
    header, else {}), uncovered the reasons the rerun cannot cover the step (an empty list:
    every failure is a test id the class rerun can run)."""
    lines = (text or '').splitlines()
    if any(SECTION.match(line) or KT_LINE.match(line) for line in lines):
        return _parse_kit_tests(lines)
    if any(RAN.match(line) for line in lines):
        return _parse_block(lines, {}, 'unittest')
    return [], ['gate.py --rerun covers kit_tests and unittest reports only, and this log is '
                'neither (a gradle or dotnet log, say)']


def _read(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()
    except (OSError, TypeError):
        return None


def cover_step(res, cwd_rel):
    """The red run's entry of one step result `res` (runner.run_steps)."""
    entry = {'name': res['name'], 'status': res['status']}
    if res['status'] != 'FAIL':
        return entry
    uncovered = ['%s: %s' % (res['name'], r) for r in res.get('reasons') or []
                 if not COVERED_REASON.match(r)]
    if cwd_rel is None:
        uncovered.append('%s: not a step whose directory is in the tree' % res['name'])
    text = _read(res.get('log'))
    if text is None:
        uncovered.append('%s: its log cannot be read' % res['name'])
        failures = []
    else:
        failures, why = parse_log(text)
        uncovered += ['%s: %s' % (res['name'], w) for w in why]
    for f in failures:
        f['cwd'] = cwd_rel
    entry.update(failures=failures, uncovered=uncovered, log=res.get('log'))
    return entry


def _cwd_rel(top, cwd):
    rel = os.path.relpath(os.path.abspath(cwd or top), os.path.abspath(top))
    if rel == os.curdir:
        return ''
    if rel.startswith(os.pardir) or os.path.isabs(rel):
        return None
    return rel.replace(os.sep, '/')


def record_red(top, cfg, head, base, started, results, chosen, log_dir):
    """Write the red run of a failed `gate.py --full` of `head` (the runner has checked the tree
    was clean, HEAD unmoved and the stash empty): its path. RerunError when it cannot."""
    try:
        commit, tree, author = gate_ok._facts(top, head)
    except gate_ok.RecordError as e:
        raise RerunError(str(e))
    cwds = {s.name: _cwd_rel(top, s.cwd) for s in chosen}
    rec = {'kind': RED, 'commit': commit, 'tree': tree, 'author': author, 'base': base,
           'config_rev': commit, 'full': True,
           'gate_config': gate_ok.config_hash(runner.gate_config(cfg)),
           'started': started, 'ended': gate_ok.utc(), 'log_dir': log_dir,
           'steps': [cover_step(r, cwds.get(r['name'])) for r in results]}
    return _write(top, rec)[0]


def uncovered_of(red):
    return [w for s in red.get('steps') or [] for w in s.get('uncovered') or []] + [
        '%s: %s with no failing test parsed' % (s['name'], s['status'])
        for s in red.get('steps') or [] if s.get('status') == 'FAIL' and not s.get('failures')
        and not s.get('uncovered')] + [
        '%s: status %r' % (s.get('name'), s.get('status')) for s in red.get('steps') or []
        if s.get('status') not in PASSING + ('FAIL', 'NOT RUN')]


# ---------------------------------------------------------------- the landing's checks

def _facts_check(root, tip, recs):
    try:
        commit, tree, author = gate_ok._facts(root, tip)
    except gate_ok.RecordError as e:
        return str(e)
    for r in recs:
        if (r.get('commit'), r.get('tree')) != (commit, tree):
            return 'a rerun record of %s names another commit or tree' % tip[:10]
        if r.get('kind') == RED and r.get('author') != author:
            return 'a red run of %s names another author' % tip[:10]
    return None


def _folder(root, tip):
    """The commit's record folder, or None when this machine has no rerun folder to find."""
    try:
        return commit_dir(root, tip)
    except RerunError:
        return None


def twice_refusal(root, tip, main):
    """Why `tip` never lands: a test failed on it and failed twice on its branch (branch_twice,
    the branch being its commits since its merge base with `main`), or its rerun records, or
    the branch's, cannot be read. None when neither, when the tip has no rerun record of its own
    (a green `gate.py --full` of a fix lands), and when the machine has no rerun folder.
    """
    if _folder(root, tip) is None:
        return None
    try:
        reds, reruns = load(root, tip)
    except RerunError as e:
        return 'its rerun records cannot be read: %s' % e
    if not reds and not reruns:
        return None
    ids, why = branch_twice(root, tip, main, reds, reruns)
    if ids is None:
        return why
    return _twice_text(ids) if ids else None


def has_records(root, tip):
    """True when `tip` has a rerun record folder with anything in it."""
    folder = _folder(root, tip)
    try:
        return folder is not None and bool(os.listdir(folder))
    except OSError:
        return False


def verify_flake(root, tip):
    """None when the latest red `gate.py --full` run of `tip` was rerun once and found a FLAKE,
    and that still holds against the records and git; else why not."""
    try:
        reds, reruns = load(root, tip)
    except RerunError as e:
        return 'its rerun records cannot be read: %s' % e
    if not reds:
        return 'no red gate.py --full run of it is recorded, so no targeted rerun can land it'
    why = _facts_check(root, tip, reds + reruns)
    if why:
        return why
    ids = twice(reds, reruns)
    if ids:
        return _twice_text(ids)
    red = reds[-1]
    if red.get('config_rev') != tip or red.get('full') is not True:
        return 'its latest red run is not a gate.py --full run of the tip'
    unc = uncovered_of(red)
    if unc:
        return 'its latest red run is uncovered (%s): only a passing gate.py --full lands it' % (
            '; '.join(unc[:3]))
    mine = [r for r in reruns if r.get('red') == red['hash']]
    if not mine:
        return 'its latest red run has no targeted rerun (gate.py --rerun)'
    if len(mine) > 1:
        return 'its latest red run has more than one rerun record'
    rr = mine[0]
    if rr.get('verdict') != FLAKE:
        return 'the targeted rerun of its latest red run said %s, not FLAKE' % rr.get('verdict')
    got = {}
    for c in rr.get('classes') or []:
        for tid, st in (c.get('ids') or {}).items():
            got[(tid, json.dumps(c.get('env') or {}, sort_keys=True))] = st
    for s in red['steps']:
        for f in s.get('failures') or []:
            if got.get((f['id'], json.dumps(f.get('env') or {}, sort_keys=True))) != 'ok':
                return 'the targeted rerun did not pass %s in the mode it failed in' % f['id']
    rerun_steps = {s.get('name'): s.get('status') for s in rr.get('steps') or []}
    for s in red['steps']:
        if s['status'] == 'NOT RUN' and rerun_steps.get(s['name']) not in PASSING:
            return 'the targeted rerun did not pass the step %s the red run never reached' % (
                s['name'])
    if any(st not in PASSING for st in rerun_steps.values()):
        return 'a step of the targeted rerun did not pass'
    return None


# ---------------------------------------------------------------- the rerun itself

def judge_class(text, rc, ids):
    """`(passed, {id: status})` of one `python -m unittest -v <class>` run. The run is whole when
    it exited 0, its summary is OK and it ran at least one test: unittest then had no failure and
    no error. An id passes in a whole run that shows it run; when the summary counts a skip or an
    expected failure, only an id shown `ok` passes. A fixture id (setUpClass and the like)
    passes when some test of its class or module passes so."""
    lines = (text or '').splitlines()
    shown = {}
    for i, line in enumerate(lines):
        m = PROGRESS.match(line)
        if not m:
            continue
        tid, _ = _test_id(m.group(1), m.group(2))
        rest = m.group(3)
        if ' ... ' not in rest and i + 1 < len(lines) and ' ... ' in lines[i + 1]:
            rest = lines[i + 1]             # a docstring line carries the status
        shown[tid] = rest.rsplit(' ... ', 1)[1].strip() if ' ... ' in rest else ''
    ran = [int(m.group(1)) for m in (RAN.match(line) for line in lines) if m]
    oks = [m for m in (OK_LINE.match(line.strip()) for line in lines) if m]
    whole = rc == 0 and bool(oks) and bool(ran) and ran[-1] >= 1
    plain = whole and oks[-1].group(0).strip() == 'OK'

    def passed(tid):
        return tid in shown and (plain or shown[tid] == 'ok')
    out = {}
    for tid in ids:
        owner, method = tid.rsplit('.', 1)
        if method in FIXTURES:
            ok = whole and any(passed(t) for t in shown if t.startswith(owner + '.'))
        else:
            ok = whole and passed(tid)
        out[tid] = 'ok' if ok else (shown.get(tid) or 'not run') if whole else 'run not ok'
    return whole and all(v == 'ok' for v in out.values()), out


def _start(launcher, argv, cwd, env, log_path):
    with open(log_path, 'wb') as log:
        try:
            p = launcher.start(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                               stderr=subprocess.STDOUT, env=env)
        except (OSError, JobError, LockError) as e:
            log.write(('rerun: could not start %s: %s\n' % (argv, e)).encode('utf-8'))
            return None
        return launcher.wait(p)


def classes_of(red):
    """[{cls, env, cwd, ids}] of the red run's failures, one per class, mode and directory."""
    out = []
    for s in red['steps']:
        for f in s.get('failures') or []:
            key = (f['cls'], json.dumps(f.get('env') or {}, sort_keys=True), f.get('cwd') or '')
            for c in out:
                if (c['cls'], json.dumps(c['env'], sort_keys=True), c['cwd']) == key:
                    if f['id'] not in c['ids']:
                        c['ids'].append(f['id'])
                    break
            else:
                out.append({'cls': f['cls'], 'env': dict(f.get('env') or {}),
                            'cwd': f.get('cwd') or '', 'ids': [f['id']]})
    return out


def _control_tree(top, cfg, base, say):
    """`(path, None)` of a throwaway detached tree at `base`, or `(None, why)`."""
    from kitlib.redfirst import run as rf      # it imports the runner, as this module does
    from kitlib.worktree import remove
    try:
        main = remove.main_checkout(top)
    except Exception as e:          # noqa: BLE001 - no control, said; never the verdict
        return None, None, 'the main checkout cannot be found: %s' % e
    path, why = rf.make_tree(main, cfg.repo['worktrees_dir'], base)
    return main, path, why


def _drop_tree(main, path, base, cfg, say):
    from kitlib.redfirst import run as rf
    wait_s = cfg.section('redfirst').get('hold_wait', 60)
    if not rf.cleanup_here(main, path, base, [], [], wait_s, say):
        say(rf.leftover_line(path, False, main))


def scope_of(top, cls):
    """The test file a class id names in the tree (`tests/gate/test_x.py`), else `tests`."""
    parts = cls.split('.')
    for n in range(len(parts), 0, -1):
        rel = '/'.join(parts[:n]) + '.py'
        if os.path.isfile(os.path.join(top, *rel.split('/'))):
            return rel
    return 'tests'


def add_line(top, branch, head, classes):
    """The `backlog.py add` line a FLAKE prints: the builder reports it; the coordinator decides
    on filing it (skill builder, step 10). The summary is a short title, within plan.SUMMARY_MAX
    (the class, cut to 60 characters, and the tip); the failing test ids go into the
    not-folded reason, which the record keeps in its body's `## Found` section."""
    ids = [t for c in classes for t in c['ids']]
    cls = classes[0]['cls'].split('.')[-1]
    name = re.sub(r'[^A-Za-z0-9]+', '-', cls).strip('-').upper()
    scopes = sorted({scope_of(top, c['cls']) for c in classes})
    more = ' and %d more' % (len(classes) - 1) if len(classes) > 1 else ''
    return ('%s add FLAKE-%s --summary "flaky: %s%s in gate.py --full at %s" --scope %s '
            '--found-on %s --why-not-folded "a flake outside this branch\'s story; it failed in '
            'gate.py --full and passed its targeted rerun: %s"'
            % (kitroot.command('backlog', top), name[:40] or 'TEST', cls[:60], more, head[:10],
               ','.join(scopes), branch or 'none', ', '.join(ids)))


def rerun_gate(root, slug=None, log_dir=None, say=None, book_kw=None):
    """`gate.py --rerun` (module docstring): an Outcome, OK for FLAKE, FAILED for REAL (or VOID:
    the tree or HEAD changed while it ran), REFUSED when nothing can be rerun, or a machine
    lock code."""
    say = say or runner._quiet
    top, why = repo.toplevel(root)
    if not top:
        return G.Outcome(G.REFUSED, ['REFUSED: %s' % why])
    try:
        cfg = kconfig.load(top)
    except ConfigError as e:
        return G.Outcome(G.REFUSED, ['REFUSED: %s' % e])
    why = stash.refusal(top)
    if why:
        return G.Outcome(G.REFUSED, ['REFUSED: %s' % why])
    head, why = refs.head(top)
    if not head:
        return G.Outcome(G.REFUSED, ['REFUSED: no HEAD commit: %s' % why])
    branch, _ = refs.current_branch(top)
    entries, why = gitstatus.status(top)
    if entries != []:
        return G.Outcome(G.REFUSED, ['REFUSED: the work tree is not clean (%s): a targeted rerun '
                                     'reruns the commit its red run gated' % (
                                         why or '%d path(s)' % len(entries))])
    try:
        reds, reruns = load(top, head)
    except RerunError as e:
        return G.Outcome(G.REFUSED, ['REFUSED: the rerun records of %s: %s' % (head[:10], e)])
    if not reds:
        return G.Outcome(G.REFUSED, ['REFUSED: no red gate.py --full run of HEAD %s is recorded '
                                     '(only a --full run of a clean tree writes one)' % head[:10]])
    why = _facts_check(top, head, reds + reruns)
    if why:
        return G.Outcome(G.REFUSED, ['REFUSED: %s' % why])
    red = reds[-1]
    if any(r.get('red') == red['hash'] for r in reruns):
        return G.Outcome(G.REFUSED, ['REFUSED: the latest red run of %s was rerun already; one '
                                     'rerun per red run: the next step is a fix, or a new '
                                     'gate.py --full' % head[:10]])
    ids, why = branch_twice(top, head, red['base'], reds, reruns)
    if ids is None:
        return G.Outcome(G.REFUSED, ['REFUSED: %s' % why])
    if ids:
        return G.Outcome(G.FAILED, ['RERUN REAL: %s; nothing was rerun' % _twice_text(ids)],
                         verdict=REAL)
    if red.get('gate_config') != gate_ok.config_hash(runner.gate_config(cfg)):
        return G.Outcome(G.REFUSED, ['REFUSED: the gate config is not the one the red run used'])
    unc = uncovered_of(red)
    if unc:
        return G.Outcome(G.REFUSED, ['REFUSED: the red run is uncovered, so only a passing '
                                     'gate.py --full lands this tip:'] + ['  %s' % u for u in unc])
    classes = classes_of(red)
    not_run = [s['name'] for s in red['steps'] if s['status'] == 'NOT RUN']
    base = red['base']
    try:
        eff = detect.effective(cfg, top, base=base)
        gate = eff.gate
        built = {s.name: s for s in S.build(gate, top, base, cfg.tools_dir, full=True)}
    except (detect.DetectError, S.GateConfigError) as e:
        return G.Outcome(G.REFUSED, ['REFUSED: %s' % e])
    missing = [n for n in not_run if n not in built]
    if missing:
        return G.Outcome(G.REFUSED, ['REFUSED: the gate has no step %s now' % ', '.join(missing)])
    steps = [built[n] for n in not_run]
    checks, options = {}, {}
    if any(s.check for s in steps):
        try:
            checks, options = policy.load_gate_checks(cfg)
        except ConfigError as e:
            return G.Outcome(G.REFUSED, ['REFUSED: %s' % e])
    changed, _ = runner.changed_since(top, base)
    name = runner.repo_name(cfg, top)
    logs = runner.new_log_dir(cfg, top, name, 'rerun-' + (branch or head[:10]), log_dir)
    started = gate_ok.utc()
    say('targeted rerun of %s at %s (red run of %s): %d class(es)%s' % (
        branch or '(detached)', head[:10], red['ended'], len(classes),
        '; steps never reached: %s' % ', '.join(not_run) if not_run else ''))
    store = (book_kw or {}).get('store') or Store()
    over = budget.refusal(top, store, name, branch or None, say)
    if over:
        return G.Outcome(G.REFUSED, over)
    gatecap.wait_turn(store, name, say)
    try:
        mine = {'branch': branch} if branch else {}
        slot = lockrun.book(gate['kind'], name, slug or runner.slug_of(branch or head[:10]),
                            **dict(book_kw or {}, gate=True, tree=red['tree'], **mine))
    except LockError as e:
        return runner.book_refusal(e, logs, base)
    done, results = [], []
    with slot:
        say(slot.held_text())
        main, tw, why = _control_tree(top, cfg, base, say)
        if why:
            say('control: no tree at the base %s (%s); the controls are not run' % (base[:10], why))
        try:
            for i, c in enumerate(classes):
                if slot.stop_asked():
                    break
                env = runner.child_env(slot.launcher)
                env.update(c['env'])
                argv = [sys.executable, '-m', 'unittest', '-v', c['cls']]
                log = os.path.join(logs, 'class-%d.log' % (i + 1))
                rc = _start(slot.launcher, argv, os.path.join(top, *c['cwd'].split('/')) if
                            c['cwd'] else top, env, log)
                passed, got = judge_class(_read(log), rc, c['ids'])
                control, clog = 'not run', None
                if tw and not slot.stop_asked():
                    clog = os.path.join(logs, 'class-%d.base.log' % (i + 1))
                    crc = _start(slot.launcher, argv, os.path.join(tw, *c['cwd'].split('/')) if
                                 c['cwd'] else tw, env, clog)
                    ctext = _read(clog)
                    cpass, _ = judge_class(ctext, crc, c['ids'])
                    control = 'green' if cpass else (
                        'absent' if ABSENT.search(ctext or '') else 'red')
                say('%-5s %s%s at the tip; control at the base %s: %s%s' % (
                    'PASS' if passed else 'FAIL', c['cls'],
                    ' (%s)' % ', '.join('%s=%s' % kv for kv in sorted(c['env'].items()))
                    if c['env'] else '', base[:10], control,
                    ' (also red at base)' if control == 'red' else ''))
                done.append(dict(c, tip='pass' if passed else 'fail', ids=got, log=log,
                                 control=control, control_log=clog))
            if steps:
                ctx = {'root': top, 'base': base, 'merged': False, 'changed': list(changed or []),
                       'options': options}
                results = runner.run_steps(slot.launcher, steps, gate, ctx, logs, say, checks,
                                           halted=slot.stop_asked)
            results += runner.window_results(slot, say)
        finally:
            if tw:
                _drop_tree(main, tw, base, cfg, say)
        if slot.stop_asked():       # no rerun record: the red run keeps its one rerun
            slot.release(1, 'cancelled')
            return runner.stopped_outcome('gate run %s was stopped' % slot.id, logs, base)
        flake = (all(c['tip'] == 'pass' for c in done) and
                 all(r['status'] in PASSING for r in results))
        slot.release(0 if flake else 1, 'done')
    after, _ = gitstatus.status(top)
    head_now, _ = refs.head(top)
    void = None
    if after != []:
        void = 'the work tree changed while the rerun ran'
    elif head_now != head:
        void = 'HEAD moved while the rerun ran'
    verdict = VOID if void else (FLAKE if flake else REAL)
    rec = {'kind': RERUN, 'commit': head, 'tree': red['tree'], 'red': red['hash'],
           'started': started, 'ended': gate_ok.utc(), 'log_dir': logs, 'base': base,
           'classes': done, 'steps': [{'name': r['name'], 'status': r['status']} for r in results],
           'verdict': verdict, 'void': void}
    lines = []
    try:
        path, _ = _write(top, rec)
        lines.append('rerun record: %s' % path)
    except RerunError as e:
        lines.append('FAILED: the rerun record could not be written: %s' % e)
        return G.Outcome(G.FAILED, ['RERUN %s, unrecorded' % verdict] + lines, verdict=verdict)
    red_base = [c['cls'] for c in done if c['control'] == 'red']
    if red_base:
        lines.append('also red at base %s: %s (a break that was there before this branch)' % (
            base[:10], ', '.join(red_base)))
    lines.append('logs: %s' % logs)
    if verdict == FLAKE:
        lines = ['RERUN FLAKE: every failing test passed at the tip, and every step the red run '
                 'never reached passed; the tip lands on this record'] + lines + [
            'report this line to your coordinator, who decides on filing it:',
            '  ' + add_line(top, branch, head, classes)]
        return G.Outcome(G.OK, lines, verdict=verdict, record=path)
    head_line = ('RERUN VOID: %s; the tip does not land on this rerun' % void if void else
                 'RERUN REAL: a failing test failed again, or a step did not pass: the failure is '
                 'real, and the tip never lands on it')
    return G.Outcome(G.FAILED, [head_line] + lines, verdict=verdict, record=path)
