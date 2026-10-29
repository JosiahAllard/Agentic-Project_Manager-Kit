"""The gate's steps run in a machine-lock slot (kitlib.lock.run.book), and the two gates built on
that: the branch gate (`gate.py`) and the merged-tree gate a landing runs (kitlib.gate.land).

Every step runs in the slot's job (its launcher): pinned to the run's cores, measured, and ended
as a tree when the run is cancelled. A step's output goes to `<log dir>/<step>.log`; the step is
judged from that log (kitlib.gate.steps.judge). No Python the gate runs writes bytecode into the
tree: a step's child process (a cmd, a requires probe, a check's run()) gets
PYTHONDONTWRITEBYTECODE=1 added to the slot's environment (child_env), and a check itself runs
under kitlib.config.policy.no_bytecode.

A check step calls the repo hook module's GATE_CHECKS[<check>](ctx). ctx is a dict:
  root      the checkout gated          base     the base commit (hash)
  merged    True on a landing's merged-tree gate
  changed   the changed paths (repo-relative, posix)
  options   `.kit.json` "hook".options  name     the step name
  log       the step's log, a text file open for append
  run       run(argv, cwd=None) -> exit code: argv in the slot's job, output into the log
It returns 0 or True to pass; any other int or False fails it; raising, or calling sys.exit with
any code, fails it with the traceback in the log.

A gate where no selected step PASSed, WARNed, FAILed or was REUSED (all skipped for a missing
`requires`) is REFUSED (nothing_ran). The branch gate writes the gate-OK record
(kitlib.records.gate_ok) for HEAD only when every selected step passed, warned, was reused or
was skipped, no --only was given, the base is the default base, the work tree was clean (no
tracked change, no untracked file) when the gate started and when it ended, HEAD did not
move, and the repository's stash stack was empty when the gate started (else REFUSED, before
--plan; kitlib.gate.stash) and when it ended: the record then names exactly the commit whose tree
was gated, and its config_rev is that commit. A REUSED step did not run in this gate: a pass of
its key is in the gate cache
(kitlib.gate.reuse), stored by an earlier gate of the same inputs. A rerun with every step
reused therefore writes a gate-OK record for a commit on which no step ran; its record says
`reused` for each step. A `--full` gate that FAILED on those same terms (no --only, the default
base, a clean tree at start and end, HEAD unmoved, the stash empty) writes a red run instead
(kitlib.gate.rerun.record_red), which `gate.py --rerun` reruns (docs/gate.md "Targeted rerun").
"""
import datetime
import os
import re
import shutil
import subprocess
import sys
import time
import traceback

from kitlib import gate as G
import kitlib.config as kconfig
from kitlib.config import policy
from kitlib.config import user as kuser
from kitlib.config.types import ConfigError
from kitlib.gate import budget
from kitlib.gate import detect
from kitlib.gate import gatecap
from kitlib.gate import gitstatus
from kitlib.gate import hookrev
from kitlib.gate import result
from kitlib.gate import stash
from kitlib.gate import steps as S
from kitlib.git import cmd, refs, repo
from kitlib.lock import run as lockrun
from kitlib.lock.store import EXIT_USAGE, LockError, Store
from kitlib.proc import process, winwatch
from kitlib.proc.job import JobError
from kitlib.records import gate_ok

NO_STEP = ('REFUSED: no gate step selected: the "gate".steps of .kit.json cover none of the %d '
           'changed path(s), or each step they select is dropped by another\'s skip_if_selected, '
           'so nothing would run; no gate-OK record is written. Give a step "always": true or '
           'globs that cover these paths')
# --full and the merged gate select every step but the manual ones, whatever changed: none left
# means every step is manual, or each is dropped by another selected step's skip_if_selected.
NO_STEP_FULL = ('REFUSED: no gate step selected: every step in the "gate".steps of .kit.json is '
                'manual, or is dropped by another selected step\'s skip_if_selected, so nothing '
                'would run; no gate-OK record is written. Give the gate a step that is not manual '
                'and that no other step\'s skip_if_selected names')
NO_CHECKS = ('GATE OK (auto-detected: no stack found, so NO checks ran; give the gate steps with a '
             '"gate" section in .kit.json or tools/gate.json)')
PROBE_TIMEOUT = 60      # seconds a `requires` probe argv may take
# A console window the gate's run opened (window_results): False, it warns; True, it fails the
# gate as a `windows` result. The one switch.
WINDOWS_FAIL = False
RESULT = {'PASS': 'pass', 'WARN': 'warn', 'SKIP': 'skip', 'REUSED': 'reused'}


def _quiet(_line):
    pass


def gate_config(cfg):
    """The value a gate-OK record's `gate_config` hashes: `.kit.json` "gate" over its defaults.
    The kit guard and the pre-push hook verify a record with this same value. An auto-detected gate
    (kitlib.gate.detect) hashes this too, not its detected steps: they are a function of the
    commit's tree (which the record names), its base's tree (`base`) and the kit (its `kit`), so
    the hash still names what decides the steps. A key of HASH_IF_SET that is unset is left out,
    so a config that sets none of them hashes as it did before the key existed: a record a kit
    without the key wrote still verifies. Each key of HASH_REMOVED is put in with the default it
    hashed as, so a record a kit that still had the key wrote verifies too."""
    gate = cfg.section('gate')
    hashed = {k: v for k, v in gate.items() if not (k in HASH_IF_SET and v is None)}
    hashed.update((k, v) for k, v in HASH_REMOVED.items() if k not in hashed)
    return hashed


# "gate" keys added after records were first written; unset, they are not in gate_config().
HASH_IF_SET = ('smoke_steps', 'docs_only')
# "gate" keys the schema no longer has (a config that sets one fails to load), with the default
# every config hashed them as while they were keys. Only the hash keeps them, never section() or
# the steps: dropping one would change every repo's hash, so every gate-OK record written on the
# other side of that kit change would fail verify_record (docs/gate.md "Gate-OK record").
HASH_REMOVED = {'python_tests': 'discover', 'python_tests_floor': None, 'trailers': None}


def gate_config_at(root, rev):
    """gate_config() of the config commit `rev` holds (kitlib.config.load_at); ConfigError when it
    cannot be read."""
    return gate_config(kconfig.load_at(root, rev))


def verify_record(root, commit):
    """gate_ok.verify() with the gate config read from the record's own `config_rev` (the commit
    whose `.kit.json` the gate ran with): `(record, None)` or `(None, why)`. Until the legacy read
    is retired a record's .git copy counts when the record folder has none (gate_ok.read)."""
    def config_at(rev):
        try:
            return gate_config_at(root, rev)
        except ConfigError as e:
            raise gate_ok.RecordError('the gate config at %s: %s' % (rev[:10], e))
    return gate_ok.verify(root, commit, config_at=config_at)


def kit_identity():
    """{'commit': the kit checkout's HEAD ('unknown' when git cannot say), 'python': the
    interpreter running the gate, 'clean': True when the kit checkout has no uncommitted or
    untracked file (None when git cannot say)}, as a gate-OK record holds it. Run from a pinned
    export of pushed main (kitlib.gate.pin), the export's commit, clean: an export holds exactly
    that commit's tracked files. The gate cache (kitlib.gate.reuse) keys steps on reuse.kit_identity instead:
    the kit's file hashes, never its commit."""
    from kitlib import pluginsource
    marker = pluginsource.read_marker(S.KIT_ROOT)
    if marker is not None and refs.is_sha(marker.get('sha')):
        return {'commit': marker['sha'],
                'python': os.path.normcase(os.path.abspath(sys.executable)), 'clean': True}
    out, _ = cmd.run_text(['rev-parse', 'HEAD'], S.KIT_ROOT)
    status, _ = cmd.run_text(['--no-optional-locks', 'status', '--porcelain'], S.KIT_ROOT)
    return {'commit': out if refs.is_sha(out) else 'unknown',
            'python': os.path.normcase(os.path.abspath(sys.executable)),
            'clean': None if status is None else not status}


def repo_name(cfg, root):
    return cfg.repo.get('name') or os.path.basename(os.path.normpath(root))


def slug_of(text):
    s = re.sub(r'[^A-Za-z0-9._-]+', '-', text or '').strip('-.')
    return s[:60] or 'gate'


def say_note(text):
    """The default `note` of log_base and its makers: one line on stderr."""
    print('gate logs: %s' % text, file=sys.stderr, flush=True)


def log_base(cfg, root, name, note=say_note):
    """Where gate runs of the repository at `root` keep their logs: `.kit.json` "landing".log_dir
    (repo-relative), else `<kit temp>/<repo name>-gate` (kitlib.config.user.work_temp of `root`:
    the checkout's drive, else %LOCALAPPDATA%/Temp on Windows; never an inherited TEMP). One rule
    for the gate that writes the logs and every tool that reads them (landed.py, which also reads
    fallback_log_base). A machine config work_temp refuses (ConfigError) gives fallback_log_base,
    and note(<why>)."""
    configured = cfg.section('landing').get('log_dir')
    if configured:
        return os.path.join(root, configured)
    try:
        kit = kuser.work_temp(root)
    except ConfigError as e:
        base = fallback_log_base(name)
        note('%s: the machine config could not be read (%s)' % (base, e))
        return base
    return os.path.join(kit, '%s-gate' % slug_of(name))


def fallback_log_base(name):
    """`<shared temp>/<repo name>-gate`: where new_log_dir puts a run's logs when the default
    log_base cannot be made, and where every default log went before work_temp."""
    return os.path.join(kuser.shared_temp(), '%s-gate' % slug_of(name))


def log_folder(branch):
    """A branch's folder name under log_base."""
    return slug_of(branch)


def new_log_dir(cfg, root, name, branch, base_dir=None, note=say_note):
    """A fresh folder for one gate run's logs: `<base>/<branch>/<time>-<pid>`. base: base_dir, else
    log_base; when the default log_base (no base_dir, no "landing".log_dir) cannot be made,
    fallback_log_base, and note(<why>)."""
    if base_dir:
        return _new_dir(os.path.join(base_dir, log_folder(branch)))
    return make_log_dir(cfg, root, name, log_folder(branch), note=note, fresh=True)


def make_log_dir(cfg, root, name, *parts, note=say_note, fresh=False):
    """`<log_base>/<parts...>`, made if missing (fresh: a new `<time>-<pid>` folder in it). When
    the default log_base cannot be made, the same under fallback_log_base, and note(<why>); a
    "landing".log_dir that cannot be made raises OSError, as does a fallback that cannot."""
    make = _new_dir if fresh else _made
    path = os.path.join(log_base(cfg, root, name, note), *parts)
    try:
        return make(path)
    except OSError as e:
        if cfg.section('landing').get('log_dir'):
            raise
        got = make(os.path.join(fallback_log_base(name), *parts))
        note('%s: %s could not be made (%s)' % (got, path, e))
        return got


def _made(path):
    os.makedirs(path, exist_ok=True)
    return path


def _new_dir(parent):
    stem = os.path.join(parent, '%s-%d' % (
        datetime.datetime.now().strftime('%Y%m%d-%H%M%S'), os.getpid()))
    path, n = stem, 0
    while os.path.exists(path):
        n += 1
        path = '%s-%d' % (stem, n)
    os.makedirs(path)
    return path


def _names(out):
    return [p for p in cmd.text(out).split('\0') if p]


def changed_since(root, base):
    """`(sorted changed paths, None)`: tracked paths differing between `base` and the work tree,
    plus untracked files git does not ignore; or `(None, why)`."""
    out, why = cmd.run(['diff', '--no-renames', '--name-only', '-z', base, '--'], root)
    if out is None:
        return None, why
    paths = set(_names(out))
    out, why = cmd.run(['ls-files', '--others', '--exclude-standard', '-z'], root)
    if out is None:
        return None, why
    return sorted(paths | set(_names(out))), None


def changed_between(root, a, b):
    out, why = cmd.run(['diff', '--no-renames', '--name-only', '-z', a, b, '--'], root)
    if out is None:
        return None, why
    return sorted(set(_names(out))), None


def default_base(cfg, root):
    """`(ref, note)`: kitlib.git.refs.default_base (`<remote>/<main>` when it resolves, else
    `<main>`; `note` says when the local main differs), or `(None, why)` when neither resolves."""
    ref, note = refs.default_base(root, cfg.remote, cfg.main_branch)
    if refs.resolve(ref, root)[0]:
        return ref, note
    return None, 'neither %s/%s nor %s resolves: pass --base' % (cfg.remote, cfg.main_branch,
                                                                  cfg.main_branch)


# ---------------------------------------------------------------- running steps

def child_env(launcher):
    """The environment a step's child process gets: the slot's (launcher.env; os.environ for a
    launcher without one) with PYTHONDONTWRITEBYTECODE=1 added. A child Python does not inherit
    sys.dont_write_bytecode, and a `__pycache__` it writes beside the repo's scripts is an
    untracked file in the gated tree, which costs the gate-OK record (policy.no_bytecode)."""
    base = getattr(launcher, 'env', None)
    env = dict(os.environ if base is None else base)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env


def unmet(step, launcher):
    """The requirements of `step` that are missing: a command name not on PATH, or a probe argv
    that does not exit 0 within PROBE_TIMEOUT (run in the slot's job)."""
    missing = []
    for r in step.requires:
        if isinstance(r, str):
            if not shutil.which(r):
                missing.append(r)
            continue
        ok = False
        try:
            p = launcher.start(list(r), cwd=step.cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=child_env(launcher))
            ctime = process.proc_ctime(p.pid)
            try:
                ok = p.wait(PROBE_TIMEOUT) == 0
            except subprocess.TimeoutExpired:
                if ctime is not None:       # the probe and everything it started
                    process.kill_tree(p.pid, ctime)
                p.kill()
                p.wait()
        except (OSError, JobError, LockError):
            ok = False
        if not ok:
            missing.append(' '.join(r))
    return missing


def _run_cmd(step, launcher, log_path, give_back=False):
    """give_back: the gate's last step may give back cpu slots it no longer needs while it runs
    (kitlib.lock.giveback); no step runs after it that could need them."""
    more = {'give_back': True} if give_back else {}
    with open(log_path, 'wb') as log:
        try:
            p = launcher.start(step.cmd, cwd=step.cwd, stdin=subprocess.DEVNULL, stdout=log,
                               stderr=subprocess.STDOUT, env=child_env(launcher), **more)
        except (OSError, JobError, LockError) as e:
            log.write(('gate: could not start %s: %s\n' % (step.cmd, e)).encode('utf-8'))
            return None
        return launcher.wait(p)


def _run_check(step, launcher, log_path, ctx, checks):
    fn = (checks or {}).get(step.check)
    with open(log_path, 'a', encoding='utf-8') as log:
        if fn is None:
            log.write('gate: the hook module provides no check %r\n' % step.check)
            return None

        def run(argv, cwd=None):
            log.flush()
            p = launcher.start(list(argv), cwd=cwd or ctx['root'], stdin=subprocess.DEVNULL,
                               stdout=log, stderr=subprocess.STDOUT, env=child_env(launcher))
            return launcher.wait(p)
        try:
            with policy.no_bytecode():   # a lazy sibling import writes no __pycache__ in the tree
                got = fn(dict(ctx, name=step.name, log=log, run=run))
        except KeyboardInterrupt:
            raise
        except SystemExit as e:     # a check never ends the gate, whatever code it exits with
            log.write('gate: check %s exited: SystemExit, code %r; a check returns, it does not '
                      'exit\n%s' % (step.check, e.code, traceback.format_exc()))
            return 1
        except BaseException:
            log.write('gate: check %s raised:\n%s' % (step.check, traceback.format_exc()))
            return 1
        if got is True:
            return 0
        if got is False:
            return 1
        if type(got) is int:
            return got
        log.write('gate: check %s returned %r; a check returns an int or a bool\n' % (step.check, got))
        return 1


def _read(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return ''


def run_steps(launcher, chosen, gate, ctx, log_dir, say=None, checks=None):
    """[{name, status, reasons, seconds, log}] for `chosen` run in order with `launcher`. status:
    PASS, WARN, SKIP (a requirement missing), FAIL, or NOT RUN (after a failure of a step that
    does not `continue`)."""
    say = say or _quiet
    results, stopped = [], None
    for step in chosen:
        log_path = os.path.join(log_dir, step.name + '.log')
        if stopped:
            results.append({'name': step.name, 'status': 'NOT RUN', 'seconds': None, 'log': None,
                            'reasons': ['%s failed first' % stopped]})
            continue
        t0 = time.monotonic()
        missing = unmet(step, launcher)
        if missing:
            status = 'FAIL' if step.requires_strict else 'SKIP'
            reasons = ['requires %s, which %s missing' % (', '.join(missing),
                                                          'is' if len(missing) == 1 else 'are')]
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write('gate: %s\n' % reasons[0])
        else:
            if step.check:
                rc = _run_check(step, launcher, log_path, ctx, checks)
            else:
                rc = _run_cmd(step, launcher, log_path, give_back=step is chosen[-1])
            if rc is None:
                status, reasons = 'FAIL', ['did not start (see its log)']
            else:
                status, reasons = S.judge(step, _read(log_path), rc, gate)
        secs = round(time.monotonic() - t0, 1)
        say('%-5s %s (%.1f s)%s' % (status, step.name, secs,
                                    ': ' + '; '.join(reasons[:3]) if reasons else ''))
        results.append({'name': step.name, 'status': status, 'reasons': reasons, 'seconds': secs,
                        'log': log_path})
        if status == 'FAIL' and not step.cont:
            stopped = step.name
    return results


def verdict(results, no_checks=False):
    """`(ok, the verdict line)`: `GATE OK ...` or `GATE FAILED: ...`; NO_CHECKS for an
    auto-detected gate that found no stack (`no_checks`)."""
    if no_checks and not results:
        return True, NO_CHECKS
    failed = [r for r in results if r['status'] == 'FAIL']
    if failed:
        first = failed[0]
        return False, 'GATE FAILED: %s (%s: %s)' % (', '.join(r['name'] for r in failed),
                                                    first['name'], first['reasons'][0])
    notes = []
    for status, word in (('WARN', 'warned'), ('SKIP', 'skipped'), ('REUSED', 'reused')):
        names = [r['name'] for r in results if r['status'] == status]
        if names:
            notes.append('%s: %s' % (word, ', '.join(names)))
    return True, 'GATE OK' + (' (%s)' % '; '.join(notes) if notes else '')


def nothing_ran(results):
    """The REFUSED line when no step PASSed, WARNed, FAILed or was REUSED, i.e. every selected
    step was skipped for a missing `requires`; else None. A REUSED step counts though it did
    not run here: a pass of its key (its inputs, argv, cwd, program, gate config and kit files)
    is in the gate cache (kitlib.gate.reuse). So a gate whose every step is reused passes, and a
    branch rerun of that kind writes a gate-OK record for a commit on which no step ran."""
    if any(r['status'] in ('PASS', 'WARN', 'FAIL', 'REUSED') for r in results):
        return None
    skipped = ['%s: %s' % (r['name'], '; '.join(r['reasons']) or 'skipped') for r in results]
    return ('REFUSED: no gate step ran: every selected step was skipped (%s); no gate-OK record is '
            'written. Install what they require, or give the gate a step that runs here'
            % '; '.join(skipped))


def record_steps(results):
    return [{'name': r['name'], 'result': RESULT[r['status']], 'seconds': r['seconds']}
            for r in results]


# ---------------------------------------------------------------- gate reuse (kitlib.gate.reuse)

def _reuse_log(logs, lines, say, mode):
    path = os.path.join(logs, 'reuse.log')
    try:
        with open(path, mode, encoding='utf-8') as f:
            f.write(''.join(line + '\n' for line in lines))
    except OSError as e:
        say('WARNING: %s could not be written: %s' % (path, e))


def _say_plan(plan, chosen, logs, say):
    """Say a reuse.Plan's lines and keep them in `<logs>/reuse.log`: {reused step: why}."""
    if plan is None:
        return {}
    reused = plan.reused
    lines = plan.lines + ['%-5s %s: %s' % ('REUSED', s.name, reused[s.name])
                          for s in chosen if s.name in reused]
    for line in lines:
        say(line)
    if lines:                       # no lines: no step asks for reuse, nothing to keep
        _reuse_log(logs, lines, say, 'w')
    return reused


def _to_run(chosen, reused):
    run = [s for s in chosen if s.name not in reused]
    if reused and all(s.name == 'setup' for s in run):
        run = []                    # setup prepares for steps, and every step is reused
    return run


def _with_reused(results, chosen, reused):
    """`results` with a REUSED result for each reused step, in the order of `chosen`, then any
    result that is no step's (window_results' `windows`)."""
    if not reused:
        return results
    by_name = {r['name']: r for r in results}
    steps = {s.name for s in chosen}
    return [by_name[s.name] if s.name in by_name else dict(
        {'name': s.name, 'status': 'REUSED', 'seconds': None, 'log': None},
        reasons=[reused[s.name]]) for s in chosen if s.name in by_name or s.name in reused] + [
        r for r in results if r['name'] not in steps]


def _keep(plan, results, logs, say):
    """Store the results a reuse.Plan asks for (its keep never raises); say its lines and add
    them to `<logs>/reuse.log`. A failure here never changes the gate's verdict."""
    if plan is None or plan.keep is None:
        return
    try:
        lines = plan.keep(results)
    except Exception as e:          # noqa: BLE001 - storing never fails the gate
        lines = ['gate: reuse: NOT STORED: %s: %s' % (type(e).__name__, e)]
    for line in lines:
        say(line)
    if lines:
        _reuse_log(logs, lines, say, 'a')


def plan_lines(chosen, why, all_steps, base_text, changed):
    lines = ['base %s; %d changed path(s)' % (base_text, len(changed))]
    lines += ['RUN   %s (%s)' % (s.name, why[s.name]) for s in chosen]
    picked = {s.name for s in chosen}
    lines += ['skip  %s' % s.name for s in all_steps if s.name not in picked]
    return lines


def gated(cfg, root, base, changed, chosen, kind, name, slug, log_dir, say, book_kw, merged=False,
          checks_rev=None, gate=None, slot=None, seen=None, capped=False, budget_branch=None):
    """Runs `chosen` in a slot of `kind`: `(results, None)`, or `(None, Outcome)` when the hook
    module or the booking refuses. The hook module's checks come from the work tree, or from
    commit `checks_rev` when given (a landing: --expect-main). `chosen` is never empty. The slot
    is booked as a gate run, so the build daemons the gate leaves that serve nothing outside its
    job are stopped at its end (kitlib.lock.site, machine_lock.daemons; kitlib.proc.daemons).
    `gate`: the gate the steps are judged by (detect.effective); default the configured one.
    `slot`: a Slot the caller already holds (a landing's, kitlib.gate.land): the steps run in it,
    nothing is booked and the slot is not released here (its holder releases it); once the steps
    finish, their build daemons are stopped (Slot.stop_daemons, as a gate's release does) and
    the rest of their tree is ended (Slot.end_tree), and an exception or Ctrl+C ends the tree
    first, so nothing the gate started runs on into the push or an undo.
    After the steps it says the run's console-window report (window_results), in a held `slot`
    too (its monitor stops there, before its daemons and tree are ended); `seen`, a dict when
    given, gets `windows`: the windows as dicts, None when nothing was watched.
    The lock run records the tree it tests (tested_tree; book's `tree`, Slot.note_tree for a held
    slot): a branch's HEAD tree, a landing's merge tree, the daily full run's commit's tree.
    `capped`: a branch gate's booking first waits for the session gate cap (kitlib.gate.gatecap,
    in the store it books in); a landing and the daily full run leave it False and never wait.
    `budget_branch`: with `capped`, the branch whose gate budget (kitlib.gate.budget) is asked
    first, before the cap is waited for or anything is booked: `(None, REFUSED Outcome)` when it
    is spent. None: not asked (a detached HEAD). The run is booked with it as book's `branch`,
    which its history line keeps, so the budget counts the run by its branch, not its slug."""
    checks, options = {}, {}
    if any(s.check for s in chosen):
        try:
            if checks_rev:
                checks, options = hookrev.load_gate_checks_at(cfg, root, checks_rev)
            else:
                checks, options = policy.load_gate_checks(cfg)
        except ConfigError as e:
            return None, G.Outcome(G.REFUSED, ['REFUSED: %s' % e])
        except SystemExit as e:     # the repo's module-level code; nothing has run yet
            return None, G.Outcome(G.REFUSED, [
                'REFUSED: the hook module exited while it was loaded (SystemExit, code %r); '
                'nothing ran' % (e.code,)])
    ctx = {'root': root, 'base': base, 'merged': merged, 'changed': list(changed),
           'options': options}
    tree = tested_tree(root)
    if slot is not None:
        say('the gate runs in the slot the landing holds (run %s)' % slot.id)
        slot.note_tree(tree)
        try:
            results = run_steps(slot.launcher, chosen, gate or cfg.section('gate'), ctx, log_dir,
                                say, checks)
        except BaseException:
            try:
                slot.end_tree()
            except BaseException:   # noqa: BLE001 - the holder's release ends the tree again
                pass
            raise
        # The steps are done: their window report first, as a booked gate says it before its
        # release; then what they left is stopped, as a gate's own release does (stop_daemons),
        # and the rest of their tree is ended (end_tree), not left running in the slot through
        # the push until the holder releases it. The slot stays held.
        results += window_results(slot, say, seen)
        slot.stop_daemons()
        slot.end_tree()
        return results, None
    if capped:
        store = (book_kw or {}).get('store') or Store()
        over = budget.refusal(root, store, name, budget_branch, say)
        if over:
            return None, G.Outcome(G.REFUSED, over, record=None,
                                   no_record='the branch gate budget')
        gatecap.wait_turn(store, name, say)
    try:
        mine = {'branch': budget_branch} if capped and budget_branch else {}
        slot = lockrun.book(kind, name, slug, **dict(book_kw or {}, gate=True, tree=tree, **mine))
    except LockError as e:
        return None, lock_refusal(e)
    with slot:
        say(slot.held_text())
        results = run_steps(slot.launcher, chosen, gate or cfg.section('gate'), ctx, log_dir, say,
                            checks)
        results += window_results(slot, say, seen)
        slot.release(0 if verdict(results)[0] and not nothing_ran(results) else 1, 'done')
    return results, None


def tested_tree(root):
    """The git tree hash of HEAD in `root` when its work tree is clean (no uncommitted or
    untracked path), else None: a gate of uncommitted files tests no commit's tree. None too
    when git cannot answer; the tree is a note on the lock run, never a reason to refuse."""
    entries, _ = gitstatus.status(root)
    if entries != []:
        return None
    tree, _ = refs.tree_of('HEAD', root)
    return tree or None


def window_results(slot, say, seen=None):
    """Stops the lock run's window monitor (kitlib.lock.run.Slot.watch_stop, kitlib.proc.winwatch)
    and says its report: `windows opened: N` and a line per window. Returns [] (a window warns),
    or with WINDOWS_FAIL a FAIL result named `windows` when the run opened one. Fills
    seen['windows'] when `seen` is a dict. Each line is said through _say_safe: it runs inside
    the held slot, where a console that cannot print a window's title (cp1252, U+2733) would
    end the run `error` though every step passed."""
    windows = slot.watch_stop()
    for line in slot.window_lines(fails=WINDOWS_FAIL):
        _say_safe(say, line)
    if seen is not None:
        seen['windows'] = None if windows is None else [w.as_dict() for w in windows]
    n = len(winwatch.counted(windows))
    if not (WINDOWS_FAIL and n):
        return []
    return [{'name': 'windows', 'status': 'FAIL', 'seconds': None, 'log': None,
             'reasons': ['%d console window(s) opened' % n]}]


def ascii_line(line):
    """`line` as text escaped to ASCII (backslashreplace): any console can print it."""
    return str(line).encode('ascii', 'backslashreplace').decode('ascii')


def _say_safe(say, line):
    """say(ascii_line(line)); a stream that cannot be written (closed, broken: OSError, or an
    encoding error) drops the line rather than raising. Any other exception from `say` is
    raised: a report line is never worth ending the run over, a bug in `say` still shows."""
    try:
        say(ascii_line(line))
    except (UnicodeError, OSError):
        pass


def lock_refusal(e):
    """The Outcome of a machine-lock LockError: its USAGE code is worded `USAGE:` (refusal-log P10;
    an unknown "gate".kind, say), any other code is the lock refusing the gate."""
    if e.code == EXIT_USAGE:
        msg = re.sub(r'^usage:\s*', '', e.message or '')
        return G.Outcome(G.USAGE, ['USAGE: the machine lock: %s (check .kit.json "gate".kind)' % msg])
    return G.Outcome(e.code, ['NOT RUN: the machine lock refused the gate: %s' % e.message])


# ---------------------------------------------------------------- the branch gate

def branch_gate(root, base=None, full=False, only=None, plan=False, slug=None, log_dir=None,
                say=None, book_kw=None):
    """_branch_gate() between the two result records of the branch at HEAD (kitlib.gate.result):
    `running` before it, the outcome's after it, or `error` when an exception, SystemExit or
    Ctrl+C escapes it (raised again, unchanged); the `error` record names the run's log folder
    once it was made, else none. None with `plan` or on a detached HEAD. The record lines are
    said as progress, never added to the outcome's lines."""
    say = say or _quiet
    run = None if plan else _result_run(root)
    if run is not None:
        say(run.start()[1])
    known = {}                      # _gate_checkout: 'log_dir', once the run's folder is made
    try:
        out = _branch_gate(root, base=base, full=full, only=only, plan=plan, slug=slug,
                           log_dir=log_dir, say=say, book_kw=book_kw, known=known)
    except BaseException as e:
        if run is not None:
            try:
                say(run.error(e, log=known.get('log_dir'))[1])
            except Exception:       # noqa: BLE001 - never replace the exception being raised
                pass
        raise
    if run is not None:
        path, line = run.finish(out)
        say(line)
        out.facts['result_record'] = path
    return out


def _result_run(root):
    """The result.Run of the checkout `root` is in (branch and HEAD now), or None."""
    try:
        top, _ = repo.toplevel(root)
        if not top:
            return None
        head, _ = refs.head(top)
        branch, _ = refs.current_branch(top)
        return result.Run.maybe(top, 'gate', branch, head)
    except Exception:               # noqa: BLE001 - no record, and the gate still runs
        return None


def _branch_gate(root, base=None, full=False, only=None, plan=False, slug=None, log_dir=None,
                 say=None, book_kw=None, known=None):
    """Gate the checkout `root` is in against `base` (default `<remote>/<main>`, else `<main>`; the
    merge base with HEAD is what is compared). Outcome codes: OK, FAILED, USAGE (an --only name
    no step has, a --base that does not resolve), REFUSED, or a machine lock code. Facts:
    verdict, results, record (the record's path or None), no_record (why none was written),
    head, base, log_dir, changed, windows (window_results: the console windows its run saw, as
    dicts; None when no step ran), source (kitlib.gate.detect: `gate.json` or `auto`, once the
    config is read). The last line, once the config is read, is the source line (detect.label).
    An auto-detected gate that found no stack runs nothing and passes, its verdict line saying
    so (NO_CHECKS). `known`, a dict: its 'log_dir' is set as soon as the run's log folder is
    made, so a caller still has it when an exception ends the run."""
    say = say or _quiet
    top, why = repo.toplevel(root)
    if not top:
        return G.Outcome(G.REFUSED, ['REFUSED: %s' % why])
    try:
        cfg = kconfig.load(top)
    except ConfigError as e:
        return G.Outcome(G.REFUSED, ['REFUSED: %s' % e])
    seen = [detect.effective(cfg, top)]
    out = _gate_checkout(top, cfg, seen, base=base, full=full, only=only, plan=plan, slug=slug,
                         log_dir=log_dir, say=say, book_kw=book_kw, known=known)
    out.facts['source'] = seen[0].source
    out.lines.append(detect.label(seen[0]))
    return out


def zero_count_warnings(gate):
    """The lines of steps.zero_count_passes(gate), or one line saying the check could not run:
    a warning only, so nothing it raises stops or fails the gate. Each line is escaped to ASCII
    (backslashreplace), so a console that cannot encode a pattern (cp1252) cannot raise either."""
    try:
        lines = list(S.zero_count_passes(gate))
    except Exception as e:          # noqa: BLE001 - a warning never fails or stops the gate
        lines = ['the zero-count "pass" check could not run: %s: %s' % (type(e).__name__, e)]
    return [ascii_line(line) for line in lines]


def _gate_checkout(top, cfg, seen, base=None, full=False, only=None, plan=False, slug=None,
                   log_dir=None, say=None, book_kw=None, known=None):
    """_branch_gate() once the config is read. seen[0]: the gate it runs (detect.effective),
    first from the checkout alone; an auto-detected one is detected again once the merge base is
    known, with the steps the merge base owes, and set back in seen[0]."""
    eff = seen[0]
    gate = eff.gate
    if gate.get('worktree_guard') and repo.classify(top).kind == repo.MAIN:
        return G.Outcome(G.REFUSED, ['REFUSED: %s is a main checkout, and "gate".worktree_guard '
                                     'gates only linked worktrees' % top])
    why = stash.refusal(top)          # before --plan too: no gate command answers past an entry
    if why:
        return G.Outcome(G.REFUSED, ['REFUSED: %s' % why])
    head, why = refs.head(top)
    if not head:
        return G.Outcome(G.REFUSED, ['REFUSED: no HEAD commit: %s' % why])
    branch, _ = refs.current_branch(top)
    default, why = default_base(cfg, top)
    if base is None:
        base = default
        if base is None:
            return G.Outcome(G.REFUSED, ['REFUSED: %s' % why])
        if why:
            say(why)                  # the local main differs from the one measured against
    base_sha, why = refs.resolve(base, top)
    if not base_sha:
        return G.Outcome(G.USAGE, ['USAGE: --base %s: %s' % (base, why)])
    partial = None
    if default is None or refs.resolve(default, top)[0] != base_sha:
        partial = '--base %s is not the default base (%s): the gate was partial' % (
            base, default or 'none resolves')
    mb, why = refs.merge_base(base_sha, head, top)
    if not mb:
        return G.Outcome(G.REFUSED, ['REFUSED: %s and HEAD have no merge base%s' % (
            base, ': %s' % why if why else '')])
    changed, why = changed_since(top, mb)
    if changed is None:
        return G.Outcome(G.REFUSED, ['REFUSED: git cannot list the changed paths: %s' % why])
    if eff.source == detect.AUTO:
        try:
            seen[0] = eff = detect.effective(cfg, top, base=mb)
        except detect.DetectError as e:
            return G.Outcome(G.REFUSED, ['REFUSED: the auto-detected gate: %s' % e])
        gate = eff.gate
        say(detect.label(eff))
    empty = detect.nothing_detected(eff)
    from kitlib.gate import reuse   # reuse imports this module
    docs = not (full or only or empty) and reuse.docs_only_change(cfg, gate, changed)
    if docs:
        say('a docs-only change: only the "docs_only" steps run (docs/gate.md "Docs-only changes")')
    try:
        all_steps = S.build(gate, top, mb, cfg.tools_dir, full=full)
        chosen, picked = S.select(gate, all_steps, changed, full=full, only=only,
                                  read=S.working_tree_reader(top), docs=docs)
    except S.UnknownStep as e:
        return G.Outcome(G.USAGE, ['USAGE: --only: %s' % e])
    except S.GateConfigError as e:
        return G.Outcome(G.REFUSED, ['REFUSED: %s' % e])
    base_text = '%s (merge base with %s)' % (mb[:10], base)
    if plan:
        return G.Outcome(G.OK, plan_lines(chosen, picked, all_steps, base_text, changed),
                         chosen=[s.name for s in chosen])
    if not chosen and not empty:
        text = NO_STEP_FULL if full and not only else NO_STEP % len(changed)
        return G.Outcome(G.REFUSED, [text], record=None,
                         no_record='no gate step selected', head=head, base=mb, changed=changed)
    entries, why = gitstatus.status(top)
    if entries is None:
        return G.Outcome(G.REFUSED, ['REFUSED: git status failed: %s' % why])
    untracked = [p for xy, p in entries if xy == '??']
    if gate.get('warn_untracked') and untracked:
        say('WARNING: %d untracked file(s) are gated but not committed: %s' % (
            len(untracked), ', '.join(untracked[:10])))
    for line in zero_count_warnings(gate):
        say('WARNING: %s' % line)
    name = repo_name(cfg, top)
    started = gate_ok.utc()
    logs = new_log_dir(cfg, top, name, branch or head[:10], log_dir)
    if known is not None:
        known['log_dir'] = logs
    say('gate %s at %s, base %s: %s' % (branch or '(detached)', head[:10], base_text,
                                        ', '.join(s.name for s in chosen) or 'no step'))
    plan = None if empty else reuse.branch_plan(top, cfg, chosen, mb, changed, full=full,
                                                only=only, head=head, branch=branch)
    keys_kit = None     # the kit before the run: a record's input keys (kitlib.gate.reuse)
    if not (only or partial or empty or docs or entries):
        try:
            keys_kit = reuse.kit_identity()
        except Exception as e:      # noqa: BLE001 - a key never fails the gate
            say(reuse.PREFIX + 'no input keys in the record: %s: %s' % (type(e).__name__, e))
    reused = _say_plan(plan, chosen, logs, say)
    run = _to_run(chosen, reused)
    results, watched = [], {}
    if run:
        # capped and budget_branch: the session gate cap (kitlib.gate.gatecap) and the branch
        # gate budget (kitlib.gate.budget) are a branch gate's only; a landing and the daily full
        # run book through gated() without them, and a fully reused gate never gets here.
        results, refused = gated(cfg, top, mb, changed, run, gate['kind'], name,
                                 slug or slug_of(branch or head[:10]), logs, say, book_kw,
                                 gate=gate, seen=watched, capped=True,
                                 budget_branch=branch or None)
        if refused is not None:
            return refused
    results = _with_reused(results, chosen, reused)
    _keep(plan, results, logs, say)
    ok, line = verdict(results, empty)
    lines = [line, 'logs: %s' % logs]
    facts = dict(verdict=line, results=results, record=None, no_record=None, head=head, base=mb,
                 log_dir=logs, changed=changed, windows=watched.get('windows'))
    if not ok:
        facts['no_record'] = 'the gate failed'
        if full and not only and not partial and not empty:
            facts['rerun_record'] = _red_record(top, cfg, head, mb, started, results, chosen,
                                                logs, entries, say)
        return G.Outcome(G.FAILED, lines, **facts)
    refused = None if empty else nothing_ran(results)
    if refused:
        facts['no_record'] = 'no gate step ran'
        return G.Outcome(G.REFUSED, [refused, 'logs: %s' % logs], **facts)
    after, _ = gitstatus.status(top)
    head_now, _ = refs.head(top)
    no = None
    if only:
        no = '--only gates part of the tree'
    elif partial:
        no = partial
    elif entries:
        no = 'the work tree had uncommitted changes when the gate started'
    elif after != []:
        no = 'the work tree changed while the gate ran'
    elif head_now != head:
        no = 'HEAD moved while the gate ran (%s -> %s)' % (head[:10], (head_now or '?')[:10])
    else:
        no = stash.appeared(top)      # empty when the gate started (above); clean and at HEAD now
    if no:
        facts['no_record'] = no
        lines.append('no gate-OK record: %s' % no)
        return G.Outcome(G.OK, lines, **facts)
    input_keys, why = reuse.record_keys(top, cfg, chosen, head, mb, results, keys_kit)
    if why:
        say(reuse.PREFIX + 'no input keys in the record: %s' % why)
    try:
        path, _rec = gate_ok.record_pass(top, head, gate_config(cfg), kit_identity(), name, started,
                                         record_steps(results), base=mb, config_rev=head,
                                         source=eff.source, full=bool(full),
                                         mode='docs' if docs else None, input_keys=input_keys)
    except gate_ok.NoRecordFolder as e:
        # Until the .git copies are retired: the .git copy is written, as before the record folder.
        path = gate_ok.legacy_path(top, head)
        facts['record'] = path
        lines.append('warning: the gate-OK record could not be written to the record folder: %s'
                     % e)
        lines.append('gate-OK record (.git copy only): %s' % path)
        return G.Outcome(G.OK, lines, **facts)
    except gate_ok.RecordError as e:
        facts['no_record'] = str(e)
        lines.append('FAILED: the gate passed, and its gate-OK record could not be written: %s' % e)
        return G.Outcome(G.FAILED, lines, **facts)
    facts['record'] = path
    lines.append('gate-OK record: %s' % path)
    return G.Outcome(G.OK, lines, **facts)


def _red_record(top, cfg, head, mb, started, results, chosen, logs, entries, say):
    """The red run of a failed `--full` gate (kitlib.gate.rerun.record_red), written on the gate-OK
    record's own terms: the tree clean when the gate started and when it ended, HEAD unmoved and
    the stash empty. Its path, or None (said). Nothing here changes the gate's verdict."""
    from kitlib.gate import rerun   # rerun imports this module
    after, _ = gitstatus.status(top)
    head_now, _ = refs.head(top)
    if entries:
        no = 'the work tree had uncommitted changes when the gate started'
    elif after != []:
        no = 'the work tree changed while the gate ran'
    elif head_now != head:
        no = 'HEAD moved while the gate ran'
    else:
        no = stash.appeared(top)
    if no:
        say('no red-run record for a targeted rerun: %s' % no)
        return None
    try:
        path = rerun.record_red(top, cfg, head, mb, started, results, chosen, logs)
    except rerun.RerunError as e:
        say('WARNING: no red-run record for a targeted rerun: %s' % e)
        return None
    say('red-run record: %s; a targeted rerun of the failing tests: gate.py --rerun '
        '(docs/gate.md "Targeted rerun")' % path)
    return path


# ---------------------------------------------------------------- the merged-tree gate

def merged_gate(root, cfg, base, full=False, slug='land', log_dir=None, say=None, book_kw=None,
                reuse=None, slot=None, tree=None, docs=False):
    """_merged_gate() with its source (kitlib.gate.detect) in facts['source'] and as the last
    line. With no "gate" section in `cfg` the gate is detected from the merged tree, plus the
    steps `base` owes (detect.effective). `slot`: the Slot the landing already holds; the steps
    run in it and no second slot is booked (gated). `tree`: the checkout holding the merge (a
    landing's landing tree, kitlib.gate.land); default `root`. `docs`: a docs-only merge may run
    only its docs steps (_merged_gate)."""
    try:
        eff = detect.effective(cfg, tree or root, base=base)
    except detect.DetectError as e:
        return G.Outcome(G.REFUSED, ['REFUSED: the auto-detected gate: %s' % e])
    if eff.source == detect.AUTO:
        (say or _quiet)(detect.label(eff))
    out = _merged_gate(root, cfg, eff, base, full=full, slug=slug, log_dir=log_dir, say=say,
                       book_kw=book_kw, reuse=reuse, slot=slot, tree=tree, docs=docs)
    out.facts['source'] = eff.source
    out.lines.append(detect.label(eff))
    return out


def _merged_choice(cfg, eff, tree, base, full, docs):
    """What _merged_gate selects at HEAD of `tree` against `base`, running nothing: (head,
    changed, chosen, docs, empty), or the REFUSED Outcome it would end with."""
    empty = detect.nothing_detected(eff)
    head, why = refs.head(tree)
    if not head:
        return G.Outcome(G.REFUSED, ['REFUSED: no HEAD commit: %s' % why])
    changed, why = changed_between(tree, base, head)
    if changed is None:
        return G.Outcome(G.REFUSED, ['REFUSED: git cannot list the merged paths: %s' % why])
    from kitlib.gate import reuse as R     # reuse imports this module; `reuse` is the planner
    docs = bool(docs) and not (full or empty) and R.docs_only_change(cfg, eff.gate, changed)
    try:
        all_steps = S.build(eff.gate, tree, base, cfg.tools_dir, merged=True, full=full)
        chosen, _ = S.select(eff.gate, all_steps, changed, full=not docs, docs=docs,
                             read=S.working_tree_reader(tree))
    except S.GateConfigError as e:
        return G.Outcome(G.REFUSED, ['REFUSED: %s' % e])
    if not chosen and not empty:
        return G.Outcome(G.REFUSED, [NO_STEP_FULL], head=head)
    return head, changed, chosen, docs, empty


def merged_steps(root, cfg, base, full=False, tree=None, docs=False):
    """What merged_gate() with these arguments would run, without running anything:
    `({'head', 'steps': [names, in order], 'docs': bool, 'source'}, None)`, or `(None, why)` when
    it would refuse. A landing whose merged gate runs on a load node checks the node's record
    against this (kitlib.loadnode.landgate)."""
    tree = tree or root
    try:
        eff = detect.effective(cfg, tree, base=base)
    except detect.DetectError as e:
        return None, 'the auto-detected gate: %s' % e
    picked = _merged_choice(cfg, eff, tree, base, full, docs)
    if isinstance(picked, G.Outcome):
        return None, '; '.join(picked.lines)
    head, _changed, chosen, docs, _empty = picked
    return {'head': head, 'steps': [s.name for s in chosen], 'docs': docs,
            'source': eff.source}, None


def _merged_gate(root, cfg, eff, base, full=False, slug='land', log_dir=None, say=None,
                 book_kw=None, reuse=None, slot=None, tree=None, docs=False):
    """Gate the merged tree at HEAD of `tree` (a landing's merge, in its landing tree; default
    `root`) against `base` (the main it was merged into). The steps run in `tree`, and their hook
    checks get it as ctx['root']; the repository's name and its log folder are `root`'s (the main
    checkout's), wherever the tree is. Every non-manual step is selected, as gate.py --full
    selects them, whatever paths base..HEAD changes: a fresh landing tree lacks the main
    checkout's ignored caches, so a step the branch's paths would not select (an asset import)
    must still run. Each runs its cmd, or its full_cmd, when it has one, with `full` (a strict
    landing). Each step's
    merged_args are appended. Outcome codes:
    OK, FAILED, REFUSED (also when no step is selected: every step manual, or each dropped by
    another's skip_if_selected) or a machine lock code; facts verdict,
    results, log_dir, head, windows (as the branch gate's), docs. Check steps run the hook module
    of commit `base`, as the config is read from it (kitlib.gate.hookrev), never the merged tree's.

    `docs` True and not `full`: when base..HEAD is a docs-only change under `cfg` (the base's;
    kitlib.gate.reuse.docs_only_change: its "gate".docs_only, never a config, hook or strict path)
    only the docs_only steps (and setup) run, and facts['docs'] is True (the landing records the
    merge with mode "docs"; docs/gate.md "Docs-only changes").

    `reuse(chosen) -> (reuse.Plan)` (kitlib.gate.reuse.planner): the steps it names are not run
    and count as REUSED; its lines, then the lines of its keep (the results it stores in the gate
    cache), are said and kept in `<log dir>/reuse.log`. `setup` runs only when some step runs,
    and no slot is booked when none does. An auto-detected gate that found no stack runs nothing
    and passes (NO_CHECKS). `slot`: a held Slot to run in instead of booking one (gated)."""
    say = say or _quiet
    tree = tree or root
    gate = eff.gate
    picked = _merged_choice(cfg, eff, tree, base, full, docs)
    if isinstance(picked, G.Outcome):
        return picked
    head, changed, chosen, docs, empty = picked
    name = repo_name(cfg, root)
    logs = new_log_dir(cfg, root, name, 'land-' + slug, log_dir)
    if docs:
        say('a docs-only merge: only the "docs_only" steps run (docs/gate.md "Docs-only changes")')
    say('merged-tree gate at %s against %s: %s' % (
        head[:10], base[:10], ', '.join(s.name for s in chosen) or 'no step'))
    plan = reuse(chosen) if reuse is not None and not empty else None
    reused = _say_plan(plan, chosen, logs, say)
    run = _to_run(chosen, reused)
    results, watched = [], {}
    if run:
        results, refused = gated(cfg, tree, base, changed, run, gate['kind'], name, slug_of(slug),
                                 logs, say, book_kw, merged=True, checks_rev=base, gate=gate,
                                 slot=slot, seen=watched)
        if refused is not None:
            return refused
    results = _with_reused(results, chosen, reused)
    _keep(plan, results, logs, say)
    ok, line = verdict(results, empty)
    refused = nothing_ran(results) if ok and not empty else None
    if refused:
        return G.Outcome(G.REFUSED, [refused, 'logs: %s' % logs], verdict=line, results=results,
                         log_dir=logs, head=head, windows=watched.get('windows'), docs=docs)
    return G.Outcome(G.OK if ok else G.FAILED, [line, 'logs: %s' % logs], verdict=line,
                     results=results, log_dir=logs, head=head, windows=watched.get('windows'),
                     docs=docs)
