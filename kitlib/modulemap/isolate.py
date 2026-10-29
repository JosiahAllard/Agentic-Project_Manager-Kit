"""The isolation run (docs/module-map.md "Isolation runs"): each module that names a `test` in
the map runs it in a sparse clone of HEAD holding only what its map entry lets it reach.

The tree (members, patterns). A module's isolation tree holds the module itself, the messaging
modules, the direct targets of every edge and ratchet from it, and then, from every module in the
set, the targets of its build edges and build ratchets, until nothing new is added. Of the root's
own files it holds the top-level files and the map's `isolate.include` paths; when an edge or
ratchet in the set reaches `(root)`, all of the root. A module not in the set is left out, even
inside a folder the tree holds.

The run (run). Each tree is `git clone --shared --no-checkout` of the repository's git common
dir, its origin removed, then checked out sparse (non-cone patterns) at HEAD's commit: only
committed HEAD is isolated, never the work tree, and nothing is ever run in the checkout itself.
A module whose test fails or times out there runs again in a full clone of the same commit,
unless `rerun` is off: passing there makes it COUPLED (it needs something its map entry does not
allow), failing there too makes it RED (its tests fail anyway), timing out there too makes it
TIMEOUT. A tree that cannot be made or a test that cannot start is ERROR. A test runs in the
machine-lock slot the caller holds (slot.start, so a lock call inside it nests) and is ended with
its tree (slot.end_tree) past its timeout or when the lock asks the run to stop.

The clones live in one run folder in the kit's temp folder for the checkout
(kitlib.config.user.make_temp, `kit-iso-<pid>-<ctime>-*`). Each clone is removed when its run
ends, the run folder at the end, and a run folder whose process is gone is removed by the next
run (sweep). The source repository's config and refs are never written: a shared clone only
reads its objects.

The tool's run (run_tool) books the machine lock for the daily_full kind, waiting up to its
max_wait (`.kit.json` "daily_full"); run as a step of daily_full.py it nests in the daily run's
slot and takes nothing more. A plan books nothing.

Nothing here prints or exits: lines go to `say`.
"""
import collections
import os
import re
import subprocess
import sys
import time

from kitlib.config import user as kuser
from kitlib.fs.rmtree import rmtree_force
from kitlib.git import cmd as gitcmd
from kitlib.git import repo as gitrepo
from kitlib.lock import run as lockrun
from kitlib.lock.store import LockError, Store
from kitlib.modulemap.load import (JAVA_IMPORTS, ROOT, TEST_TIMEOUT, MapError, _norm)
from kitlib.proc import process

PASS, COUPLED, RED, TIMEOUT, FAIL, ERROR = 'PASS', 'COUPLED', 'RED', 'TIMEOUT', 'FAIL', 'ERROR'
OK, FAILED, REFUSED = 0, 1, 2
SLUG = 'module-isolation'           # the lock run's slug
PREFIX = 'kit-iso-'                 # a run folder: kit-iso-<pid>-<ctime or x>-<random>
FOLDER_RE = re.compile(r'kit-iso-(\d+)-(\d+|x)-.+')
CLONE_TIMEOUT = 900                 # seconds a clone or its checkout may take
TAIL_LINES = 2000                   # the most lines of a test's output a run echoes
POLL_S = 1.0                        # seconds between a running test's timeout checks
END_WAIT_S = 10                     # seconds a test ended with its tree gets to exit
TAG = 'module_map isolate:'

Entry = collections.namedtuple('Entry', 'module held patterns test')
Got = collections.namedtuple('Got', 'code how secs note')       # how: done timeout stopped error
Result = collections.namedtuple('Result', 'module status line')


def _build_ratchet(r):
    """True when ratchet `r` counts build crossings (every java-imports one does)."""
    return r.get('detector') == JAVA_IMPORTS or not r.get('kinds') or 'build' in r['kinds']


def members(mm, name):
    """The module names whose paths `name`'s isolation tree holds; ROOT among them when an edge
    or ratchet in the set reaches the root (module docstring, "The tree")."""
    held = {name} | set(mm.messaging)
    held |= {e['to'] for e in mm.edges if e['from'] == name}
    held |= {r['to'] for r in mm.ratchets if r['from'] == name}
    todo = sorted(held)
    while todo:
        x = todo.pop()
        more = {e['to'] for e in mm.edges if e['from'] == x and e['kind'] == 'build'}
        more |= {r['to'] for r in mm.ratchets if r['from'] == x and _build_ratchet(r)}
        for y in sorted(more - held):
            held.add(y)
            todo.append(y)
    return held


def _pattern(path, keep):
    """One sparse-checkout line for `path` (repo-relative, `/`): anchored, a trailing space kept."""
    tail = len(path) - len(path.rstrip(' '))
    path = path.rstrip(' ') + '\\ ' * tail
    return ('/' if keep else '!/') + path


def patterns(mm, held):
    """The non-cone sparse-checkout lines for a tree holding the modules `held`: the root's files
    first (all of the root with ROOT in `held`, else the top-level files), then every module path
    (kept or left out) and the isolate.include paths, shallowest first, so a deeper path's line
    comes later and wins (git takes the last line that matches)."""
    whole_root = ROOT in held
    lines = ['/*'] if whole_root else ['/*', '!/*/']
    rules = [(p, n in held) for n in sorted(mm.modules) for p in mm.modules[n]]
    if not whole_root:
        rules += [(p, True) for p in mm.isolate_include]
    for p, keep in sorted(rules, key=lambda r: (r[0].count('/'), r[0].lower())):
        lines.append(_pattern(p, keep))
    return lines


def test_of(mm, name):
    """`{cmd, cwd, timeout}` of `name`'s test, defaults filled in."""
    t = mm.tests[name]
    return {'cmd': list(t['cmd']), 'cwd': _norm(t.get('cwd') or ''),
            'timeout': t.get('timeout') or TEST_TIMEOUT}


def argv_of(cmd, python, root, module):
    """`cmd` with {python}, {root} (the tree's root) and {module} filled in."""
    return [a.replace('{python}', python).replace('{root}', root).replace('{module}', module)
            for a in cmd]


def plan(mm, only=None):
    """The Entry of every module with a test, by name; only `only` when given. MapError when
    `only` is no module, or one with no test."""
    names = sorted(mm.tests)
    if only is not None:
        if only not in mm.modules:
            raise MapError('--module %s: the map has no such module' % only)
        if only not in mm.tests:
            raise MapError('--module %s: the module has no test in the map' % only)
        names = [only]
    out = []
    for n in names:
        held = members(mm, n)
        out.append(Entry(n, held, patterns(mm, held), test_of(mm, n)))
    return out


def _held_text(entry):
    rest = sorted(m for m in entry.held if m != entry.module)
    return ', '.join([entry.module] + rest)


def plan_lines(mm, entries):
    """What each module's run would hold and run; the modules with no test."""
    lines = []
    if not entries:
        lines.append('%s no module in the map names a test; nothing to run' % TAG)
    for e in entries:
        lines.append('%s %s: holds %s; runs %s in %s; timeout %ds'
                     % (TAG, e.module, _held_text(e), ' '.join(e.test['cmd']),
                        e.test['cwd'] or 'the tree root', e.test['timeout']))
        lines.append('%s %s: sparse patterns: %s' % (TAG, e.module, ' '.join(e.patterns)))
    untested = sorted(set(mm.modules) - set(mm.tests))
    if untested:
        lines.append('%s not isolated (no test): %s' % (TAG, ', '.join(untested)))
    return lines


# --- the run folder ---------------------------------------------------------------------------

def sweep(parent, keep=None, alive=process.alive):
    """Remove each run folder in `parent` whose process (pid and creation time in its name) is
    gone, but `keep`; the number removed."""
    try:
        names = os.listdir(parent)
    except OSError:
        return 0
    gone = 0
    for name in names:
        m = FOLDER_RE.fullmatch(name)
        path = os.path.join(parent, name)
        if not m or (keep and os.path.normcase(path) == os.path.normcase(keep)):
            continue
        if alive(int(m.group(1)), None if m.group(2) == 'x' else int(m.group(2))):
            continue
        rmtree_force(path, ignore_errors=True)
        gone += not os.path.lexists(path)
    return gone


def make_folder(top):
    """`(run folder, notes)`: a new run folder for the checkout `top`; the folders of runs whose
    process is gone are removed first."""
    ctime = process.own_ctime()
    prefix = '%s%d-%s-' % (PREFIX, os.getpid(), 'x' if ctime is None else int(ctime))
    folder, note = kuser.make_temp(top, prefix)
    notes = [note] if note else []
    gone = sweep(os.path.dirname(folder), keep=folder)
    if gone:
        notes.append('removed %d run folder(s) a run that ended without its cleanup left' % gone)
    return folder, notes


# --- the clones ---------------------------------------------------------------------------------

def _git(args, cwd, timeout=gitcmd.GIT_TIMEOUT):
    """None, or why `git <args>` failed."""
    r = gitcmd.call(args, cwd, timeout=timeout)
    if r.rc == 0:
        return None
    return 'git %s: %s' % (' '.join(args[:2]),
                           (r.err or gitcmd.text(r.out) or 'exit %s' % r.rc).strip()[-400:])


def make_clone(common, sha, dest, lines=None):
    """Clone the git common dir `common` into `dest` (shared, no checkout, origin removed) and
    check out `sha` there, sparse to `lines` when given. None, or why it could not."""
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    conf = ['core.longpaths=true', 'core.hooksPath=.git/kit-no-hooks']
    if lines is not None:
        conf += ['core.sparseCheckout=true', 'core.sparseCheckoutCone=false']
    args = ['clone', '-q', '--shared', '--no-checkout']
    for c in conf:
        args += ['--config', c]
    why = _git(args + ['--', common, dest], parent, timeout=CLONE_TIMEOUT)
    if why:
        return why
    why = _git(['remote', 'remove', 'origin'], dest)
    if why:
        return why
    if lines is not None:
        info = os.path.join(dest, '.git', 'info')
        try:
            os.makedirs(info, exist_ok=True)
            with open(os.path.join(info, 'sparse-checkout'), 'w', encoding='utf-8',
                      newline='\n') as fh:
                fh.write('\n'.join(lines) + '\n')
        except OSError as e:
            return 'cannot write the sparse-checkout patterns: %s' % e
    return _git(['checkout', '-q', '--detach', sha], dest, timeout=CLONE_TIMEOUT)


# --- one test -----------------------------------------------------------------------------------

def run_test(slot, argv, cwd, env, log, timeout, clock=time.monotonic, poll=POLL_S):
    """Got(code, how, secs, note) of `argv` run in `cwd` in the slot's job, its output in `log`.
    how: 'done' (code its exit code), 'timeout' or 'stopped' (the lock's stop; code None, its
    tree ended), 'error' (it could not start; note says why)."""
    t0 = clock()
    if not os.path.isdir(cwd):
        return Got(None, 'error', 0, 'its folder %s is not in the tree' % cwd)
    with open(log, 'w', encoding='utf-8', errors='replace') as fh:
        try:
            p = slot.start(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=fh,
                           stderr=subprocess.STDOUT)
        except (OSError, LockError) as e:           # JobError is an OSError
            return Got(None, 'error', clock() - t0, 'could not start %s: %s' % (argv[0], e))
        deadline = t0 + timeout
        while True:
            try:
                code = p.wait(poll)
                return Got(code, 'done', clock() - t0, None)
            except subprocess.TimeoutExpired:
                pass
            how = ('stopped' if slot.stop_asked() else
                   'timeout' if clock() >= deadline else None)
            if how:
                slot.end_tree()
                try:
                    p.wait(END_WAIT_S)
                except subprocess.TimeoutExpired:
                    pass
                return Got(None, how, clock() - t0, None)


def _got_text(got, timeout):
    if got.how == 'done':
        return 'exit %d, %ds' % (got.code, got.secs)
    if got.how == 'timeout':
        return 'timed out after %ds, ended with its tree' % timeout
    if got.how == 'stopped':
        return 'ended with its tree: the machine lock asked the run to stop'
    return got.note


def echo(say, module, label, log, tail=TAIL_LINES):
    """Say the last `tail` lines of `log` between two marker lines."""
    try:
        with open(log, encoding='utf-8', errors='replace') as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        say('%s %s: the %s run\'s output cannot be read: %s' % (TAG, module, label, e))
        return
    cut = ' (the last %d of %d lines)' % (tail, len(lines)) if len(lines) > tail else ''
    say('%s --- %s, %s run: output%s ---' % (TAG, module, label, cut))
    for line in lines[-tail:]:
        say(line)
    say('%s --- end of %s, %s run ---' % (TAG, module, label))


class Run(object):
    """One isolation run: the clones' source, the commit, the run folder and the slot."""

    def __init__(self, common, sha, folder, slot, say, python=None, rerun=True,
                 tail=TAIL_LINES, clock=time.monotonic, poll=POLL_S):
        self.common, self.sha, self.folder, self.slot, self.say = common, sha, folder, slot, say
        self.python = python or sys.executable
        self.rerun, self.tail, self.clock, self.poll = rerun, tail, clock, poll

    def attempt(self, entry, label, lines):
        """Got of `entry`'s test in a new clone (`label` 'isolated': sparse to `lines`; 'full':
        the whole tree), the clone removed after; the output echoed when it did not pass."""
        dest = os.path.join(self.folder, 'iso' if lines is not None else 'full', entry.module)
        log = os.path.join(self.folder, '%s-%s.log' % (entry.module, label))
        try:
            why = make_clone(self.common, self.sha, dest, lines)
            if why:
                return Got(None, 'error', 0, 'the %s tree could not be made: %s' % (label, why))
            t = entry.test
            cwd = os.path.join(dest, *t['cwd'].split('/')) if t['cwd'] else dest
            env = gitcmd.scrubbed_env(self.slot.env)
            got = run_test(self.slot, argv_of(t['cmd'], self.python, dest, entry.module), cwd,
                           env, log, t['timeout'], clock=self.clock, poll=self.poll)
        finally:
            rmtree_force(dest, ignore_errors=True)
        if not (got.how == 'done' and got.code == 0) and os.path.isfile(log):
            echo(self.say, entry.module, label, log, self.tail)
        return got

    def module(self, entry):
        """The Result of `entry`'s module."""
        name, timeout = entry.module, entry.test['timeout']
        iso = self.attempt(entry, 'isolated', entry.patterns)
        if iso.how == 'done' and iso.code == 0:
            return Result(name, PASS, '%s %s PASS (%s)' % (TAG, name, _got_text(iso, timeout)))
        said = _got_text(iso, timeout)
        if iso.how in ('error', 'stopped'):
            return Result(name, ERROR, '%s %s ERROR: isolated: %s' % (TAG, name, said))
        if not self.rerun:
            return Result(name, FAIL, '%s %s FAIL: isolated: %s; not run again in a full clone, '
                                      'so coupled or red is not told' % (TAG, name, said))
        full = self.attempt(entry, 'full', None)
        both = 'isolated: %s; full clone: %s' % (said, _got_text(full, timeout))
        if full.how == 'done' and full.code == 0:
            return Result(name, COUPLED, '%s %s COUPLED: it passes in a full clone, so it needs '
                                         'something its tree leaves out (%s; its tree holds %s)'
                          % (TAG, name, both, _held_text(entry)))
        if full.how == 'done':
            return Result(name, RED, '%s %s RED: it fails in a full clone too (%s)'
                          % (TAG, name, both))
        if full.how == 'timeout':
            return Result(name, TIMEOUT, '%s %s TIMEOUT: it times out in a full clone too (%s)'
                          % (TAG, name, both))
        return Result(name, ERROR, '%s %s ERROR: %s' % (TAG, name, both))

    def all(self, entries):
        """The Result of every entry; once the lock asks the run to stop, the rest are ERROR."""
        out = []
        for e in entries:
            if self.slot.stop_asked():
                r = Result(e.module, ERROR, '%s %s ERROR: not run: the machine lock asked the '
                                            'run to stop' % (TAG, e.module))
            else:
                self.say('%s %s: holds %s; running %s' % (TAG, e.module, _held_text(e),
                                                         ' '.join(e.test['cmd'])))
                r = self.module(e)
            self.say(r.line)
            out.append(r)
        return out


def summary(results):
    """`(code, line)`: OK when every module passed, else FAILED."""
    counts = collections.Counter(r.status for r in results)
    text = ', '.join('%d %s' % (counts[s], s) for s in (PASS, COUPLED, RED, TIMEOUT, FAIL, ERROR)
                     if counts[s])
    bad = sorted(r.module for r in results if r.status != PASS)
    if not bad:
        return OK, '%s OK: %s' % (TAG, text or 'nothing to run')
    return FAILED, '%s FAILED: %s (%s)' % (TAG, text, ', '.join(bad))


def run(top, mm, slot, say, only=None, rerun=True, python=None, tail=TAIL_LINES,
        clock=time.monotonic, poll=POLL_S):
    """Run every planned module's test in its isolation tree (module docstring, "The run").
    0 when each passed (or none has a test), 1 otherwise; MapError for a bad `only`."""
    entries = plan(mm, only)
    if not entries:
        say('%s no module in the map names a test; nothing to run' % TAG)
        return OK
    sha, why = gitcmd.run_text(['rev-parse', '--verify', 'HEAD^{commit}'], top)
    common, cwhy = gitrepo.common_dir(top)
    if not sha or not common:
        say('%s ERROR: the commit to isolate cannot be read: %s' % (TAG, why or cwhy))
        return FAILED
    try:
        folder, notes = make_folder(top)
    except (OSError, RuntimeError) as e:
        say('%s ERROR: no run folder could be made: %s' % (TAG, e))
        return FAILED
    for n in notes:
        say('%s note: %s' % (TAG, n))
    say('%s %d module(s) at %s: %s' % (TAG, len(entries), sha[:12],
                                        ', '.join(e.module for e in entries)))
    try:
        results = Run(common, sha, folder, slot, say, python=python, rerun=rerun, tail=tail,
                      clock=clock, poll=poll).all(entries)
    finally:
        rmtree_force(folder, ignore_errors=True)
    code, line = summary(results)
    say(line)
    return code


def book_slot(cfg, top, say, book=None, store=None):
    """The machine-lock Slot for an isolation run: the daily_full kind, waiting up to its
    max_wait. LockError when the lock refuses."""
    sec = cfg.section('daily_full')
    kw = {'wait_s': sec['max_wait'], 'cmd': ['module_map.py', '--isolate']}
    if book is None:
        book = lockrun.book
        store = store or Store(say=lambda text: say('%s lock: %s' % (TAG, text)))
    if store is not None:
        kw['store'] = store
    repo = cfg.repo.get('name') or os.path.basename(os.path.normpath(top))
    return book(sec['kind'], repo, SLUG, **kw)


def run_tool(top, cfg, mm, say, plan_only=False, only=None, rerun=True, book=None, store=None,
             python=None):
    """The tool's --isolate: the plan (books nothing), or the run in a booked slot. Exit code:
    0 OK or a plan, 1 a module did not pass, 2 the lock refused. MapError for a bad `only`."""
    entries = plan(mm, only)
    if plan_only:
        for line in plan_lines(mm, entries):
            say(line)
        return OK
    if not entries:
        say('%s no module in the map names a test; nothing to run' % TAG)
        return OK
    try:
        slot = book_slot(cfg, top, say, book=book, store=store)
    except LockError as e:
        say('%s NOT RUN: the machine lock refused the run (exit %d): %s' % (TAG, e.code, e.message))
        return REFUSED
    with slot:
        held = getattr(slot, 'held_text', None)
        if held is not None:
            say('%s lock: %s' % (TAG, held()))
        code = run(top, mm, slot, say, only=only, rerun=rerun, python=python)
        slot.release(code, 'done')
    return code
