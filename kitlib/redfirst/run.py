"""A red-first run (tools/redfirst.py, docs/redfirst.md): the named test files are COMMITTED files at
`head`, checked out into a throwaway DETACHED worktree at the pre-change `base`, run there, and
judged (kitlib.redfirst.verdict). Nothing is edited in place: the caller's worktree is only read.

The throwaway tree is `<main checkout>/<worktrees_dir>/redfirst-<base10>-<pid>`. It is removed on
every exit this process controls - pass, fail, error, Ctrl-C - by putting back the base's version
of every file it checked out, then kitlib.worktree.remove's checks (status, links, holders, the
registration) and its git_remove, never forced. A tree still held after `"redfirst".hold_wait`
seconds is left registered and intact, named with the worktree_remove command that removes it,
and the exit is 4.

Interrupts: a Ctrl-C while the tests run stops them and goes to the cleanup; one while the tree is
made or filled is held until that is done. The cleanup ignores Ctrl-C and runs in a child process
on its own hidden console (a new session elsewhere), every git of it included: a console Ctrl-C
reaches every process on the console, and a `git worktree remove` killed partway is the run that
destroys the entry. A hard kill of this process runs no cleanup; the leftover then shows in
`worktree_remove.py --check`.

Runners: `.kit.json` "redfirst".runners as written; unwritten, the schema default plus a glob for
the tools folder (runners_for). A path no runner takes is refused with the globs tried (select).

Output goes through `say(line)`; nothing here prints or exits.
"""
import datetime
import json
import os
import signal
import subprocess
import sys
import time

from kitlib.config import load as load_config
from kitlib.config.types import ConfigError
from kitlib.gate import runner as grunner
from kitlib.git import batch, cmd
from kitlib.git import refs as gitrefs
from kitlib.git import repo as gitrepo
from kitlib.redfirst import verdict as V
from kitlib.worktree import holders, remove

PREFIX = 'redfirst-'
HOLD_STEP_S = 5
CHILD_FLAG = '--cleanup-child'
SIGNALS = tuple(s for s in (signal.SIGINT, getattr(signal, 'SIGBREAK', None)) if s is not None)
OWN_CONSOLE = ({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt'
               else {'start_new_session': True})
TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'tools', 'redfirst.py')


def git(cwd, *args):
    """`(rc, stdout, stderr)` of a git started on its own console (OWN_CONSOLE)."""
    r = cmd.subprocess_run(('git', '-C', cwd) + args, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', env=remove.git_env(), **OWN_CONSOLE)
    return r.returncode, (r.stdout or '').strip(), (r.stderr or '').strip()


def rev(cwd, name):
    """`name`'s commit; V.Usage when it names none or git gives no answer.

    GOTCHA: KIT-GIT-EXIT1-ANSWERS-2 - this asked `rev-parse --verify --quiet`, whose "no such
    commit" is exit 1 with no output, a killed git's shape (gitrefs.KILLED_SHAPE), so a kill read
    as "does not name a commit". gitrefs.resolve(known=True) tells the two apart."""
    out, why = gitrefs.resolve(name, cwd, known=True)
    if out is None:
        raise V.Usage('refused: whether %s names a commit in %s could not be read: %s'
                      % (name, cwd, why))
    if not out:
        raise V.Usage('usage: %s does not name a commit in %s' % (name, cwd))
    return out


def object_types(cwd, specs):
    """{spec: git's object type, or None when missing} for every `<rev>:<path>`, in one cat-file."""
    found, why = batch.cat_file(list(dict.fromkeys(specs)), cwd, env=remove.git_env())
    if found is None:
        raise V.Usage('refused: git cat-file failed in %s: %s' % (cwd, why))
    return {s: (v[0] if v else None) for s, v in found.items()}


# ---- interrupts ---------------------------------------------------------------------------------
RAISE, HOLD, CLEAN = 'raise', 'hold', 'clean'


class Interrupts(object):
    """SIGINT and SIGBREAK while a run lasts. The handler only records, and raises
    KeyboardInterrupt only in RAISE mode (the test runs) and at most once: it drops to HOLD as it
    raises. HOLD notes the signal for the next safe point; CLEAN counts it and carries on."""

    def __init__(self):
        self.mode, self.seen, self.during_cleanup, self.old = HOLD, 0, 0, {}

    def handler(self, signum, frame):
        self.seen += 1
        if self.mode == CLEAN:
            self.during_cleanup += 1
        elif self.mode == RAISE:
            self.mode = HOLD
            raise KeyboardInterrupt('signal %d' % signum)

    def install(self):
        for s in SIGNALS:
            self.old[s] = signal.signal(s, self.handler)

    def restore(self):
        for s, h in self.old.items():
            if h is not None:
                signal.signal(s, h)
        self.old = {}


# ---- the throwaway tree -------------------------------------------------------------------------
def make_tree(main, worktrees_dir, base):
    path = os.path.join(main, worktrees_dir, '%s%s-%d' % (PREFIX, base[:10], os.getpid()))
    if os.path.lexists(path):
        return None, 'the throwaway path %s exists already' % path
    rc, out, err = git(main, 'worktree', 'add', '--detach', path, base)
    if rc != 0:
        return None, 'git worktree add failed: %s' % (err or out)
    return path, None


def copy_in(tw, head, files):
    rc, out, err = git(tw, 'checkout', head, '--', *files)
    if rc != 0:
        raise RuntimeError('git checkout %s -- %s failed: %s' % (head[:10], ' '.join(files), err or out))


def put_back(tw, base, new, had):
    """Undo copy_in: the base's version of each file it had; each file it never had out of the
    index and off the disk. Returns the problems."""
    problems = []
    if had:
        rc, out, err = git(tw, 'checkout', base, '--', *had)
        if rc != 0:
            problems.append('git checkout %s -- %s: %s' % (base[:10], ' '.join(had), err or out))
    if new:
        rc, out, err = git(tw, 'rm', '-q', '--cached', '--ignore-unmatch', '--', *new)
        if rc != 0:
            problems.append('git rm --cached %s: %s' % (' '.join(new), err or out))
        for f in new:
            full = os.path.join(tw, *f.split('/'))
            try:
                if os.path.isfile(full):
                    os.remove(full)
            except OSError as e:
                problems.append('%s: %s' % (full, e))
    return problems


def remove_tree(ctx, tw, wait_s, step_s=HOLD_STEP_S, sleep=time.sleep):
    """kitlib.worktree.remove's checks, then its git_remove; never forced. True when gone."""
    work, disposable, ignored, why = remove.dirty(ctx, tw)
    if why:
        ctx.say('leftover  %s: its status could not be read (%s); not removed' % (tw, why))
        return False
    left = list(work) + list(disposable) + list(ignored)
    if left:
        ctx.say('leftover  %s holds files the run left (%s); not removed' % (tw, holders.listed(left)))
        return False
    links, why = holders.links_inside(tw)
    if why or links:
        ctx.say('leftover  %s: %s; not removed' % (tw, why or 'it holds links: ' + '; '.join(links)))
        return False
    deadline = time.monotonic() + wait_s
    held = holders.blockers(tw)
    while held and time.monotonic() < deadline:
        sleep(step_s)
        held = holders.blockers(tw)
    if held:
        ctx.say('leftover  %s is held after %d s; git was NOT run' % (tw, wait_s))
        ctx.describe(tw, held)
        return False
    entry = remove.registrations(ctx.main).get(holders.canon(tw))
    if entry is None:
        ctx.say('leftover  %s is not a registered worktree; not removed' % tw)
        return False
    return remove.git_remove(ctx, tw, entry[0])


def cleanup_here(main, tw, base, new, had, wait_s, say, step_s=HOLD_STEP_S):
    """put_back, then remove_tree, in this process. True when the tree is gone; never raises."""
    try:
        problems = put_back(tw, base, new, had)
        for p in problems:
            say('failed    putting back: %s' % p)
        if problems:
            return False
        ctx, refusal = remove.open_ctx(main)
        if refusal:
            for line in refusal.lines:
                say(line)
            return False
        try:
            return remove_tree(ctx, tw, wait_s, step_s)
        finally:
            for line in ctx.out:
                say(line)
    except Exception as e:  # noqa: BLE001  (the caller reports a leftover, exit 4)
        say('failed    cleanup: %s: %s' % (type(e).__name__, e))
        return False


def cleanup_in_child(main, tw, base, new, had, wait_s, say):
    """cleanup_here in a child process on its own console, its lines relayed through `say`. Falls
    back to this process when the child will not start."""
    job = json.dumps({'main': main, 'tw': tw, 'base': base, 'new': new, 'had': had,
                      'wait_s': wait_s})
    try:
        proc = subprocess.Popen([sys.executable, TOOL, CHILD_FLAG], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=cmd.scrubbed_env(), **OWN_CONSOLE)
    except OSError as e:
        say('note      the cleanup process would not start (%s); cleaning up in this one' % e)
        return cleanup_here(main, tw, base, new, had, wait_s, say)
    with proc:
        try:
            proc.stdin.write(job.encode('utf-8'))
            proc.stdin.close()
        except OSError:
            pass
        for raw in proc.stdout:
            say(raw.decode('utf-8', 'replace').rstrip('\r\n'))
        rc = proc.wait()
    if rc in (0, V.EXIT_LEFTOVER):
        return rc == 0
    say('failed    the cleanup process exited %d' % rc)
    return False


def cleanup_child(stdin, say):
    """`redfirst.py --cleanup-child`: one cleanup_here from the JSON job on `stdin` (bytes). Exit 0
    when the tree is gone, 4 when not. The caller ignores Ctrl-C first."""
    try:
        job = json.loads(stdin.decode('utf-8'))
        ok = cleanup_here(job['main'], job['tw'], job['base'], job['new'], job['had'],
                          job['wait_s'], say)
    except Exception as e:  # noqa: BLE001
        say('failed    the cleanup process: %s: %s' % (type(e).__name__, e))
        ok = False
    return 0 if ok else V.EXIT_LEFTOVER


def leftover_line(tw, landed, tools_dir):
    name = os.path.basename(tw)
    verb = 'python %s/worktree_remove.py %s' % (tools_dir.replace('\\', '/').rstrip('/'), name)
    return ('leftover  %s stays registered; once nothing holds it: %s'
            % (tw, verb if landed else verb + ' --abandon "red-first throwaway"'))


# ---- running ------------------------------------------------------------------------------------
def default_run(argv, cwd, env, log):
    """Run one selector's command into `log`; its exit code (127: could not start)."""
    full_env = cmd.scrubbed_env()
    full_env.update(env)
    with open(log, 'w', encoding='utf-8', errors='replace') as fh:
        try:
            return subprocess.run(argv, cwd=cwd, env=full_env, stdout=fh,
                                  stderr=subprocess.STDOUT).returncode
        except OSError as e:
            fh.write('redfirst: could not start %s: %s\n' % (argv[0], e))
            return 127


def run_one(sel, n, tw, log_dir, files, run):
    """(verdict, reason, log) for one selector. Its log and .trx from an earlier run in the same
    log dir are deleted first: only this run's results are read."""
    argv, cwd, env = V.command(sel, tw, log_dir, n)
    log = os.path.join(log_dir, '%d-%s-%s.log' % (n, sel.kind, V.stem(sel.path)))
    for stale in (log, V.trx_path(sel, log_dir, n) if sel.kind == 'dotnet' else None):
        try:
            if stale and os.path.lexists(stale):
                os.remove(stale)
        except OSError as e:
            return V.ERROR, 'an older result %s could not be deleted: %s' % (stale, e), log
    started = time.time() - 2          # file times are coarser than the clock
    rc = run(argv, cwd, env, log)
    try:
        with open(log, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    except OSError as e:
        return V.ERROR, 'the run left no log (exit %d): %s' % (rc, e), log
    v, reason = V.judge(sel, rc, text, tw, log_dir, n, files, started)
    return v, reason, log


class Plan(object):
    """Everything prepare() checked before anything is made."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def default_log_dir(cfg, main, note=grunner.say_note):
    """A new `<gate log base>/redfirst/<time>-<pid>` folder (kitlib.gate.runner.make_log_dir:
    under fallback_log_base, with a note, when the default base cannot be made). When neither can
    be made, that path unmade: execute reports it."""
    name = grunner.repo_name(cfg, main)
    try:
        return grunner.make_log_dir(cfg, main, name, 'redfirst', note=note, fresh=True)
    except OSError:
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        return os.path.join(grunner.log_base(cfg, main, name, note), 'redfirst',
                            '%s-%d' % (stamp, os.getpid()))


def prepare(where, tests, also=(), base=None, head='HEAD', log_dir=None):
    """A Plan, or V.Usage. `where`: a directory of the linked worktree the tests come from."""
    place = gitrepo.classify(where)
    if place.kind == gitrepo.MAIN:
        raise V.Usage('refused: %s is the main checkout; name your task worktree with -C <worktree>'
                      % place.top)
    if place.kind != gitrepo.LINKED or not place.top:
        raise V.Usage('usage: %s is in no linked worktree git could name (%s%s)'
                      % (where, place.kind, ': ' + place.detail if place.detail else ''))
    top = place.top
    try:
        main = remove.main_checkout(top)
        cfg = load_config(main)
    except remove.GitReadError as e:
        raise V.Usage('refused: cannot find the main checkout: %s' % e)
    except ConfigError as e:
        raise V.Usage('refused: kit configuration: %s' % e)
    section = cfg.section('redfirst')
    upstream = 'refs/remotes/%s/%s' % (cfg.remote, cfg.main_branch)
    head = rev(top, head)
    if base:
        base = rev(top, base)
    else:
        rc, out, err = git(top, 'merge-base', head, upstream)
        if rc != 0 or not out:
            raise V.Usage('usage: no --base given and the merge-base with %s could not be read '
                          '(%s); pass --base <commit>' % (upstream, err or 'exit %d' % rc))
        base = out
    if base == head:
        raise V.Usage('refused: --base and --head are the same commit %s' % base[:10])
    inside, why = gitrefs.is_ancestor(base, head, top)
    if inside is None:
        raise V.Usage('refused: whether base %s is an ancestor of head %s cannot be told: %s'
                      % (base[:10], head[:10], why))
    if not inside:
        raise V.Usage('refused: base %s is not an ancestor of head %s' % (base[:10], head[:10]))
    runners, written = runners_for(cfg)
    sels = [select(t, runners, written, top, head) for t in tests]
    files = []
    for f in [s.path for s in sels] + [p.replace('\\', '/') for p in also]:
        if f not in files:
            files.append(f)
    types = object_types(top, ['%s:%s' % (r, f) for r in (head, base) for f in files])
    new, had = [], []
    for f in files:
        at_head, at_base = types.get('%s:%s' % (head, f)), types.get('%s:%s' % (base, f))
        if at_head != 'blob':
            raise V.Usage('refused: %s is not committed at head %s%s'
                          % (f, head[:10], ' (it is a %s there)' % at_head if at_head else ''))
        if at_base not in (None, 'blob'):
            raise V.Usage('refused: %s is a %s at base %s, not a file' % (f, at_base, base[:10]))
        (had if at_base else new).append(f)
    if head == rev(top, 'HEAD'):
        rc, out, err = git(top, 'status', '--porcelain', '--', *files)
        if rc != 0:
            raise V.Usage('refused: git status failed in %s: %s' % (top, err))
        if out:
            raise V.Usage('refused: uncommitted changes to the files under test (commit them '
                          'first; the evidence must be about a commit):\n%s' % out)
    # No answer counts as not landed: the leftover line then names the --abandon command, which
    # keeps the tree's commits under a tag (the safe advice when it cannot be told).
    landed = gitrefs.is_ancestor(base, upstream, top)[0] is True
    if not log_dir:
        log_dir = default_log_dir(cfg, main)
    return Plan(top=top, main=main, base=base, head=head, sels=sels, files=files, new=new, had=had,
                landed=landed, log_dir=os.path.abspath(log_dir),
                worktrees_dir=os.path.normpath(cfg.repo['worktrees_dir']),
                tools_dir=cfg.tools_dir, hold_wait=section['hold_wait'])


# ---- which runner -------------------------------------------------------------------------------
KEY = '`.kit.json` "redfirst".runners'
# Appended to the runners for one parse: a valid path that reaches it was taken by no real runner.
_NOTHING_TOOK_IT = {'glob': '*', 'kind': 'gradle', 'dir': '.', 'project': ':'}


def runners_for(cfg):
    """`(runners, written)`. `written`: `.kit.json` holds "redfirst".runners, used as they are.
    Unwritten: the schema default, then one python runner for test files of the tools folder
    ("repo".tools_dir), which is their PYTHONPATH (the layout of kitlib/pytests/runner.py)."""
    runners = list(cfg.section('redfirst')['runners'])
    if cfg.raw('redfirst').get('runners') is not None:
        return runners, True
    tools = cfg.tools_dir.replace('\\', '/').strip('/')
    if tools and tools != '.':
        derived = {'glob': tools + '/*test_*.py', 'kind': 'python', 'pythonpath': tools}
        if not any(r['glob'].lower() == derived['glob'].lower() for r in runners):
            runners.append(derived)
    return runners, False


def no_runner_text(text, runners, written, head, committed):
    """The refusal for a `--tests` path no runner takes: every glob tried, how many files
    committed at `head` each matches (`committed`: their paths, or None when unread), and the key
    to set."""
    lines = ['refused: --tests %r: no runner takes it; nothing was run' % text]
    if not runners:
        lines.append('  %s is an empty list' % KEY)
    for r in runners:
        if committed is None:
            found = ''
        else:
            n = sum(1 for f in committed if V.matches(f, r['glob']))
            found = ': matches %s committed at head %s' % ('%d file(s)' % n if n else 'no file',
                                                           head[:10])
        lines.append('  glob %r (%s)%s' % (r['glob'], r['kind'], found))
    if written:
        lines.append('  these are the globs %s holds' % KEY)
    else:
        lines.append('  %s is not set, so these are the defaults (the tools-folder glob comes '
                     'from "repo".tools_dir)' % KEY)
    lines.append('  set %s to an entry whose glob takes the file (docs/redfirst.md)' % KEY)
    return '\n'.join(lines)


def select(text, runners, written, top, head):
    """The Selector for one `--tests` text, or V.Usage; a path no runner takes gets no_runner_text."""
    sel = V.parse_selector(text, list(runners) + [_NOTHING_TOOK_IT])
    if sel.runner is not _NOTHING_TOOK_IT:
        return sel
    rc, out, _ = git(top, 'ls-tree', '-r', '--name-only', '-z', head)
    committed = [f for f in out.split('\0') if f] if rc == 0 else None
    raise V.Usage(no_runner_text(text, runners, written, head, committed))


FINAL = {V.EXIT_RED: 'REDFIRST OK - every named test is red at %s',
         V.EXIT_GREEN: 'REDFIRST NOT RED - a named test passes at %s',
         V.EXIT_UNDECIDED: 'REDFIRST UNDECIDED - see the NONE/ERROR/stopped lines',
         V.EXIT_LEFTOVER: 'REDFIRST LEFTOVER - verdicts above, the throwaway tree was not removed'}


def execute(plan, say, run=default_run, cleanup=None, ints=None):
    """Make the tree, run every selector, clean up. Returns the exit code; never raises."""
    cleanup = cleanup or cleanup_in_child
    ints = ints or Interrupts()
    tw, cleaned = None, None
    try:
        ints.install()                       # HOLD: a Ctrl-C from here waits for the tree
        try:
            os.makedirs(plan.log_dir, exist_ok=True)
        except OSError as e:
            say('failed    the log directory %s: %s' % (plan.log_dir, e))
            say('REDFIRST UNDECIDED - nothing was made')
            return V.EXIT_UNDECIDED
        say('base      %s' % plan.base)
        say('head      %s (test files from here)' % plan.head)
        say('logs      %s' % plan.log_dir)
        tw, why = make_tree(plan.main, plan.worktrees_dir, plan.base)
        if tw is None:
            say('failed    %s' % why)
            say('REDFIRST UNDECIDED - no throwaway worktree')
            return V.EXIT_UNDECIDED
        say('tree      %s (detached at the base)' % tw)
        verdicts = []
        try:
            try:
                copy_in(tw, plan.head, plan.files)     # in HOLD: a killed checkout leaves index.lock
                ints.mode = RAISE
                if ints.seen:
                    raise KeyboardInterrupt
                for n, sel in enumerate(plan.sels, 1):
                    v, reason, log = run_one(sel, n, tw, plan.log_dir, plan.files, run)
                    verdicts.append(v)
                    say('%-6s %s  %s  %s' % (v, sel.text, reason, log))
            finally:
                ints.mode = HOLD
        except KeyboardInterrupt:
            say('stopped   interrupted; removing the throwaway tree')
            verdicts.append(V.ERROR)
        except Exception as e:  # noqa: BLE001
            say('failed    %s' % (e if isinstance(e, RuntimeError) else '%s: %s' % (type(e).__name__, e)))
            verdicts.append(V.ERROR)
        ints.mode = CLEAN
        say('cleanup   putting back the base files and removing the tree; Ctrl-C is held until it ends')
        try:
            cleaned = bool(cleanup(plan.main, tw, plan.base, plan.new, plan.had, plan.hold_wait, say))
        except Exception as e:  # noqa: BLE001
            say('failed    cleanup: %s: %s' % (type(e).__name__, e))
            cleaned = False
        ints.mode = HOLD
        if ints.during_cleanup:
            say('note      %d interrupt(s) arrived during the cleanup; it ran to its end'
                % ints.during_cleanup)
        if not cleaned:
            say(leftover_line(tw, plan.landed, plan.tools_dir))
        rc = V.exit_code(verdicts, cleaned)
        say(FINAL[rc] % plan.base[:10] if '%s' in FINAL[rc] else FINAL[rc])
        return rc
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001  (never Python's 1 = GREEN)
        say('failed    unexpected %s: %s' % (type(e).__name__, e))
        if tw and not cleaned:
            say(leftover_line(tw, plan.landed, plan.tools_dir))
            say('REDFIRST LEFTOVER - an unexpected error; the throwaway tree may still stand')
            return V.EXIT_LEFTOVER
        say('REDFIRST UNDECIDED - an unexpected error')
        return V.EXIT_UNDECIDED
    finally:
        ints.restore()
