"""kitlib.dailyfull: the daily full run (tools/daily_full.py, docs/daily-full.md).

Every non-manual `.kit.json` "gate" step, run in full on `"daily_full".ref` in a detached worktree
of its own, in one machine-lock slot of `"daily_full".kind` (kitlib.gate.runner.gated: no lock code
of its own). A failing step does not stop the run: every step runs. Each run writes
`<out>/<stamp>/result.json`; summary_lines reads them back. One run at a time per tree: the tree's
lock (kitlib.dailyfull.treelock) is held from before the fetch until the result is written.

Opt-in: nothing runs unless `"daily_full".enabled` is true in the main checkout's `.kit.json`. The
kit never schedules it; a repository that wants it daily registers that itself.

Output goes through `say(line)`; nothing here prints or exits.
"""
import datetime
import json
import os

from kitlib import gate as G
from kitlib.config import load as load_config
from kitlib.config.types import ConfigError
from kitlib.dailyfull.treelock import TreeBusy, TreeLock, TreeLockError, describe, lock_name
from kitlib.gate import detect
from kitlib.gate import runner
from kitlib.gate import steps as S
from kitlib.git import cmd as gitcmd
from kitlib.git import repo
from kitlib.lock import store as lockstore

RESULT = 'result.json'
SCHEMA = 1
SLUG = 'daily-full'
UNCHANGED = 'unchanged'    # the skip cause: the ref is where the last run that ran passed
FETCH_TIMEOUT = 300       # seconds: a fetch, a worktree add / switch writing a tree, or a status over a big tree
OFF = ('REFUSED: the daily full run is off: set "daily_full".enabled to true in .kit.json to use '
       'it (opt-in; the kit never schedules it)')
# A lock refusal is a skip (nothing ran), with this cause; any other refusal is an error.
SKIP_CAUSES = {lockstore.EXIT_BUSY: 'busy', lockstore.EXIT_TIMEOUT: 'timeout',
               lockstore.EXIT_REQUIRE: 'lock-unavailable', lockstore.EXIT_RESERVED: 'reserved',
               lockstore.EXIT_HOLD: 'hold'}


def _quiet(_line):
    pass


def now_text():
    return datetime.datetime.now().isoformat(timespec='seconds')


def settings(cfg, main):
    """The run's resolved settings: enabled, ref, worktree (absolute), kind, max_wait,
    stale_hours, out (the results folder)."""
    s = dict(cfg.section('daily_full'))
    s['ref'] = s.get('ref') or '%s/%s' % (cfg.remote, cfg.main_branch)
    s['worktree'] = os.path.normpath(os.path.join(
        main, s.get('worktree') or os.path.join(cfg.repo['worktrees_dir'], SLUG)))
    s['out'] = os.path.join(runner.log_base(cfg, main, runner.repo_name(cfg, main)), 'daily')
    return s


def git(cwd, *args):
    """`(rc, stdout, stderr)`, stripped. Through kitlib.git.cmd.call: the network reads get its
    NETWORK_ENV (the fetch fails rather than wait on a credential prompt or a stalled transfer),
    and every call has a timeout - GIT_TIMEOUT, or FETCH_TIMEOUT for a fetch, the
    worktree add / switch that writes a tree and a status over a big tree - so a hung git is a failed step, never a hang. A git
    that did not run or timed out is rc -1 with the reason in stderr."""
    sub = gitcmd.subcommand(args)
    slow = sub in gitcmd.NETWORK_READS or sub in ('worktree', 'switch', 'status')
    r = gitcmd.call(list(args), cwd, timeout=FETCH_TIMEOUT if slow else gitcmd.GIT_TIMEOUT)
    if r.rc is None:
        return -1, '', r.err
    return r.rc, gitcmd.text(r.out).strip(), r.err


def same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def tree_refusal(main, wt, git=git):
    """Why the existing directory `wt` may not be moved, or None. It must be a linked worktree
    of `main` that git lists, whose own top is `wt`, with nothing uncommitted: the run never
    switches a main checkout, a folder inside one, or a tree holding someone's changes."""
    if same_path(wt, main):
        return ('"daily_full".worktree %s is the main checkout; name a folder of its own '
                '(default <worktrees_dir>/%s)' % (wt, SLUG))
    rc, top, err = git(wt, 'rev-parse', '--show-toplevel')
    if rc != 0 or not top or not same_path(top, wt):
        return ('%s exists but is not a worktree of its own (git top: %s); move it aside or '
                'name another "daily_full".worktree' % (wt, top or err or 'none'))
    rc, out, err = git(main, 'worktree', 'list', '--porcelain')
    if rc != 0:
        return 'git worktree list failed in %s: %s' % (main, err or 'exit %d' % rc)
    listed = [line[len('worktree '):] for line in out.splitlines() if line.startswith('worktree ')]
    if listed and same_path(listed[0], wt):
        return '%s is the main checkout of its repository; it is never moved' % wt
    if not any(same_path(x, wt) for x in listed[1:]):
        return ('%s is not a linked worktree of %s (not in git worktree list); move it aside or '
                'name another "daily_full".worktree' % (wt, main))
    rc, out, err = git(wt, 'status', '--porcelain')
    if rc != 0:
        return 'git status failed in %s: %s' % (wt, err or 'exit %d' % rc)
    if out:
        return ('%s has uncommitted files, left as they are (the run never cleans a tree): %s'
                % (wt, ', '.join(line[2:].strip() for line in out.splitlines())))
    return None


def prepare(main, wt, ref, remote, git=git):
    """`(sha, None)` with `wt` detached at `ref` after a fetch of `remote` (the worktree is added
    the first time), or `(None, why)`. An existing `wt` is moved only past tree_refusal."""
    if same_path(wt, main):
        return None, tree_refusal(main, wt, git)
    rc, _, err = git(main, 'fetch', remote, '--quiet')
    if rc != 0:
        return None, 'git fetch %s failed: %s' % (remote, err or 'exit %d' % rc)
    if os.path.isdir(wt):
        why = tree_refusal(main, wt, git)
        if why:
            return None, why
        rc, _, err = git(wt, 'switch', '--detach', ref)
    else:
        rc, _, err = git(main, 'worktree', 'add', '--detach', wt, ref)
    if rc != 0:
        return None, 'could not put %s at %s: %s' % (wt, ref, err or 'exit %d' % rc)
    rc, sha, err = git(wt, 'rev-parse', 'HEAD')
    if rc != 0 or not sha:
        return None, 'could not read HEAD of %s: %s' % (wt, err or 'exit %d' % rc)
    return sha, None


def plan(cfg, wt, sha):
    """The steps a full run takes at `wt`: every non-manual gate step (and setup), each running
    its `daily_cmd` when it has one, else its `full_cmd`, else its `cmd` (kitlib.gate.steps
    argv_of), each set to continue past a failure. With no "gate" section, the steps detected
    in `wt` (kitlib.gate.detect). GateConfigError passes through."""
    gate = detect.effective(cfg, wt).gate
    chosen, _ = S.select(gate, S.build(gate, wt, sha, cfg.tools_dir, full=True, daily=True), [],
                         full=True)
    return [s._replace(cont=True) for s in chosen]


def _write(log_dir, result):
    with open(os.path.join(log_dir, RESULT), 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=1)
    return result


def write_result(log_dir, sha, started, steps, error=None):
    """A run that ran (or failed to start its steps: `error`). red: the failing steps' names, plus
    `infra` for an error, or when steps were planned and none ran (the gate's nothing_ran rule:
    every step SKIP is never green). `skipped` lists the steps that did not run."""
    if steps and not error:
        error = runner.nothing_ran(steps)
    red = [s['name'] for s in steps if s['status'] == 'FAIL'] + (['infra'] if error else [])
    return _write(log_dir, {'schema': SCHEMA, 'sha': sha, 'started': started,
                            'finished': now_text(), 'verdict': 'red' if red else 'green',
                            'error': error, 'steps': steps, 'red': red,
                            'skipped': [s['name'] for s in steps if s['status'] == 'SKIP']})


def write_skip(log_dir, started, reason, cause):
    """A run that ran nothing because the machine lock refused it."""
    return _write(log_dir, {'schema': SCHEMA, 'sha': None, 'started': started,
                            'finished': now_text(), 'verdict': 'skipped', 'reason': reason,
                            'cause': cause, 'error': None, 'steps': [], 'red': []})


def tree_lock(main, wt):
    """`(TreeLock, None)` for the worktree `wt` of the repository at `main` (its file in the git
    common dir), or `(None, why)`."""
    common, why = repo.common_dir(main)
    if not common:
        return None, 'no git common dir for %s: %s' % (main, why)
    return TreeLock(os.path.join(common, lock_name(wt)), 'daily full run of %s' % wt), None


def unchanged_since_green(out, sha):
    """The directory of the latest run that ran (a skip is not one) if it was green at `sha`, else
    None: the commit has nothing new to test. A red run, or a green one at another commit, is None."""
    ran = [r for r in results(out) if r.get('verdict') != 'skipped']
    if ran and ran[-1].get('verdict') == 'green' and ran[-1].get('sha') == sha:
        return ran[-1]['dir']
    return None


def run(main, cfg, out=None, stamp=None, say=_quiet, book_kw=None, git=git, force=False):
    """One daily full run of the repository whose main checkout is `main` (config `cfg`). Outcome
    codes: OK (green, or skipped by the lock), FAILED (a step red), USAGE (off, or the lock
    refused the call itself), REFUSED (the tree or its steps could not be readied), BUSY (another
    run holds the tree: nothing was touched or recorded).

    The tree's lock (treelock) is taken before the fetch and the switch and held until the result
    is written, on every exit, an exception included.

    Unless `force`, a ref at the commit the latest run that ran passed green is a recorded skip
    (cause `unchanged`, exit OK): the result folders are the state, there is no other file."""
    s = settings(cfg, main)
    if not s['enabled']:
        return G.Outcome(G.USAGE, [OFF])
    lock, why = tree_lock(main, s['worktree'])
    if lock is not None:
        try:
            lock.acquire()
        except TreeBusy as e:
            return G.Outcome(G.BUSY, ['daily: busy: %s (%s, lock %s); this run left the tree '
                                      'alone and recorded nothing'
                                      % (e, e.record.get('what') or s['worktree'], e.path)])
        except TreeLockError as e:
            lock, why = None, str(e)
    try:
        return _run_held(main, cfg, s, out, stamp, say, book_kw, git, lock, why, force)
    finally:
        if lock is not None:
            lock.release()


def _run_held(main, cfg, s, out, stamp, say, book_kw, git, lock, lock_why, force=False):
    stamp = stamp or datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    if out:
        log_dir = os.path.join(out, stamp)
        os.makedirs(log_dir, exist_ok=True)
    else:                       # s['out'], or the shared temp's when it cannot be made
        log_dir = runner.make_log_dir(cfg, main, runner.repo_name(cfg, main), 'daily', stamp,
                                      note=lambda t: say('daily: note: gate logs: %s' % t))
        out = os.path.dirname(log_dir)      # the results folder this run is in, fallback included
    started = now_text()

    def error(code, why, sha=None):
        write_result(log_dir, sha, started, [], why)
        return G.Outcome(code, ['daily: RED - %s' % why, 'logs: %s' % log_dir], log_dir=log_dir)

    if lock is None:
        return error(G.REFUSED, 'the tree lock could not be taken: %s' % lock_why)
    if lock.took_over:
        say('daily: took over the tree lock from a run that is gone (%s)' % describe(lock.took_over))
    sha, why = prepare(main, s['worktree'], s['ref'], cfg.remote, git=git)
    if why:
        return error(G.REFUSED, why)
    green_dir = None if force else unchanged_since_green(out, sha)
    if green_dir:
        reason = '%s is still %s, green in %s' % (s['ref'], sha[:10], green_dir)
        write_skip(log_dir, started, reason, UNCHANGED)
        return G.Outcome(G.OK, ['daily: SKIPPED (unchanged) - %s; --force runs it anyway' % reason,
                                'logs: %s' % log_dir], log_dir=log_dir, skipped=UNCHANGED)
    try:
        tested = load_config(s['worktree'])           # the ref's own gate, as it would land
        chosen = plan(tested, s['worktree'], sha)
    except (ConfigError, S.GateConfigError) as e:
        return error(G.REFUSED, 'the gate at %s: %s' % (sha[:10], e), sha)
    if not chosen:
        return error(G.REFUSED, 'the gate at %s has no non-manual step' % sha[:10], sha)
    say('daily: %s at %s, %d step(s), logs in %s' % (s['ref'], sha[:10], len(chosen), log_dir))
    kw = dict(book_kw or {})
    kw.setdefault('wait_s', s['max_wait'])
    results, refused = runner.gated(tested, s['worktree'], sha, [], chosen, s['kind'],
                                    runner.repo_name(cfg, main), SLUG, log_dir, say, kw)
    if refused is not None:
        cause = SKIP_CAUSES.get(refused.code)
        if cause is None:
            write_result(log_dir, sha, started, [], '; '.join(refused.lines))
            return G.Outcome(refused.code, refused.lines + ['logs: %s' % log_dir], log_dir=log_dir)
        reason = '; '.join(refused.lines)
        write_skip(log_dir, started, reason, cause)
        return G.Outcome(G.OK, ['daily: SKIPPED (%s) - %s' % (cause, reason), 'logs: %s' % log_dir],
                         log_dir=log_dir, skipped=cause)
    steps = [{'name': r['name'], 'status': r['status'], 'reasons': r['reasons'],
              'seconds': r['seconds'], 'log': r['log']} for r in results]
    result = write_result(log_dir, sha, started, steps)
    line = ('daily: RED %s' % ', '.join(result['red']) if result['red'] else
            'daily: GREEN at %s' % sha[:10])
    if result['error']:
        line += ' - %s' % result['error']
    return G.Outcome(G.FAILED if result['red'] else G.OK, [line, 'logs: %s' % log_dir],
                     log_dir=log_dir, result=result)


# ---------------------------------------------------------------- reading results
def results(out):
    """Every readable result under `out`, oldest first (by stamp folder), each with its `dir`."""
    try:
        names = sorted(os.listdir(out))
    except OSError:
        return []
    found = []
    for name in names:
        path = os.path.join(out, name, RESULT)
        try:
            with open(path, encoding='utf-8') as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(r, dict):
            r['dir'] = os.path.join(out, name)
            found.append(r)
    return found


def age_hours(result, now=None):
    try:
        t = datetime.datetime.fromisoformat(result.get('finished') or result.get('started'))
    except (TypeError, ValueError):
        return None
    return ((now or datetime.datetime.now()) - t).total_seconds() / 3600.0


def summary_lines(out, stale_hours, now=None):
    """What a session start or preflight prints about the daily run: one status line, and the
    failing steps when the last run is red. Pure but for reading `out`."""
    every = results(out)
    if not every:
        return ['daily     no daily full run recorded yet (tools/daily_full.py; the kit never '
                'schedules it)']
    skips = []
    for r in reversed(every):
        if r.get('verdict') != 'skipped':
            break
        skips.append(r)
    ran = [r for r in every if r.get('verdict') != 'skipped']
    age = age_hours(every[-1], now)
    stale = (' - STALE (over %d h): is its schedule running?' % stale_hours
             if age is not None and age > stale_hours else '')
    lines = []
    if skips:
        s = skips[-1]
        calm = all(x.get('cause') in ('reserved', UNCHANGED) for x in skips)
        lines.append('daily     %s at %s, %d in a row: %s%s' % (
            ('skipped (unchanged since the last green run)' if skips[0].get('cause') == UNCHANGED
             else 'skipped (owner reservation)') if calm
            else 'SKIPPED (not green: nothing ran)',
            os.path.basename(s['dir']), len(skips), skips[0].get('reason') or '?', stale))
        if not ran:
            return lines
        stale = ''
    last = ran[-1]
    lines.append('%s %s %s at %s (%s)%s' % (
        '  last ran' if skips else 'daily    ', 'RED' if last.get('red') else 'GREEN',
        (last.get('sha') or '?')[:10], os.path.basename(last['dir']), last['dir'], stale))
    for st in last.get('steps') or ():
        if st.get('status') == 'FAIL':
            lines.append('            failed: %s %s' % (st.get('name'), st.get('log')))
    if last.get('error'):
        lines.append('            failed: %s' % last['error'])
    return lines
