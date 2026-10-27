"""Safe git invocation: every kit git child goes through `call()`.

- The child's environment drops GIT_DIR, GIT_WORK_TREE, GIT_COMMON_DIR and
  GIT_CEILING_DIRECTORIES (`SCRUB_ENV`) unless the caller passes its own `env`. An inherited
  GIT_DIR would make git answer about another repository, and GIT_CEILING_DIRECTORIES set to a
  main checkout makes git say "not a git repository" there. A guard must judge the real tree,
  and the guard and the writer must judge the same one.
- The child's environment sets LC_ALL=C and LANGUAGE=C (`LOCALE_ENV`), the caller's `env` too, so
  git's messages stay English: callers match stderr text (load._git_show, repo.classify).
- Every call has a timeout (`GIT_TIMEOUT` seconds unless the caller passes another, or None for
  none). A hung git (a lock file held elsewhere) becomes a failed call, never a hang.
- Every git child starts in its own job object (kitlib.proc.job, no breakaway), and a timeout ends
  the job: the whole tree, not just the process started here. Under PowerShell `git` is
  Git/cmd/git.exe, a launcher whose child (the real git) holds the pipes; killing only the
  launcher left that child running and the call blocked on its pipes until it ended. The job is
  not kill-on-close, so what a finished git leaves running (a background daemon) lives on.
- A network read (`fetch`, `ls-remote`: `NETWORK_READS`) also gets `NETWORK_ENV`, the caller's
  `env` too: GIT_TERMINAL_PROMPT=0 and GCM_INTERACTIVE=never, so a remote that wants credentials
  fails instead of waiting on a prompt nobody can answer (the first covers git's own terminal
  prompt, the second Git Credential Manager's window; an ssh passphrase prompt is NOT covered:
  forcing `ssh -o BatchMode=yes` through GIT_SSH_COMMAND would override the user's core.sshCommand,
  GIT_SSH and ssh config handling, which this module cannot see), and GIT_HTTP_LOW_SPEED_LIMIT/TIME (git's http.lowSpeedLimit/Time),
  so a transfer stalled below 1000 bytes/s for 60 seconds fails instead of hanging until the
  call's timeout (or forever, under timeout=None). A push keeps the user's own credential prompts.
- From a parent with no console (the guard hook under pythonw.exe, a scheduled task), git starts
  with CREATE_NO_WINDOW (kitlib.proc.job.no_window_flags), so a git or ssh console prompt goes to
  a hidden console and waits until the call's timeout. A caller that can prompt must run from a
  console; none does today.
- Output stays bytes. `text()` is the one decoding: UTF-8 with errors replaced, CRLF and lone CR
  read as LF. The locale's code page is never used.
- A failure is `(None, why)`, never an empty answer: "git said nothing" and "git did not run" are
  different facts.
- A child whose subcommand is not a known read (kitlib.git.cache.READS) forgets this process's
  kitlib.git.cache memo before it starts and again after it ends.
"""
import collections
import os
import subprocess
import threading

from kitlib.proc import job as _job
from kitlib.proc import spawn as _spawn

from . import cache

SCRUB_ENV = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_CEILING_DIRECTORIES')
LOCALE_ENV = {'LC_ALL': 'C', 'LANGUAGE': 'C'}
NETWORK_READS = ('fetch', 'ls-remote')
NETWORK_ENV = {'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never',
               'GIT_HTTP_LOW_SPEED_LIMIT': '1000', 'GIT_HTTP_LOW_SPEED_TIME': '60'}
# git's global options that take a separate value: skipped to find the subcommand.
_VALUE_OPTIONS = ('-c', '-C', '--git-dir', '--work-tree', '--namespace', '--config-env')
GIT_TIMEOUT = 30          # seconds
KILL_GRACE = 5            # seconds a killed tree's pipes get to end before the call gives up on them
_DEFAULT = object()

# rc is None when git did not run at all (not found, timed out); out is bytes, err is text.
Result = collections.namedtuple('Result', 'rc out err')


def scrubbed_env(environ=None):
    """A copy of `environ` (default: this process's) without the SCRUB_ENV variables."""
    env = os.environ if environ is None else environ
    return {k: v for k, v in env.items() if k.upper() not in SCRUB_ENV}


def subcommand(args):
    """The git subcommand in `args` (global options before it skipped), or ''."""
    args = [str(a) for a in args]
    i = 0
    while i < len(args):
        a = args[i]
        if a in _VALUE_OPTIONS:
            i += 2
        elif a.startswith('-'):
            i += 1
        else:
            return a
    return ''


def network_env(args, env):
    """`env` (a mapping, left unchanged) with NETWORK_ENV set when `args` is a network read; a
    new dict either way. For the few git children that do not go through call()."""
    out = dict(env)
    if subcommand(args) in NETWORK_READS:
        out.update(NETWORK_ENV)
    return out


def child_env(env=None, args=()):
    """The environment a git child gets: `env` (None: scrubbed_env()) with LOCALE_ENV set, and
    NETWORK_ENV too when `args` is a network read."""
    out = network_env(args, scrubbed_env() if env is None else env)
    out.update(LOCALE_ENV)
    return out


def fake_active():
    """True when this process runs the kit's unit tests on the fake git: KIT_FAKE_GIT=1 in its
    environment, which kit_tests and tests/__init__.py set and production never does (docs/test-tools.md
    "Fake git")."""
    return os.environ.get('KIT_FAKE_GIT') == '1'


def call(args, cwd, stdin=None, timeout=_DEFAULT, env=None):
    """`Result(rc, out_bytes, err_text)` for `git <args>` run in `cwd`. `rc` is None when git
    could not be started or did not answer within `timeout` (default GIT_TIMEOUT; None waits
    forever); `err` then says which. `env` None means scrubbed_env(); either way child_env()
    sets the C locale.

    Under fake_active() (tests only) the answer comes from kitlib.git.fake in this process; no
    git starts, and what the fake cannot do raises fake.Unsupported. A test that swapped `_run`
    keeps its stub."""
    if _run is _REAL_RUN and fake_active():
        return _fake_call(args, cwd, stdin, env)
    limit = GIT_TIMEOUT if timeout is _DEFAULT else timeout
    cache.child(subcommand(args))       # a git that may write forgets kitlib.git.cache, before and after
    try:
        rc, out, err = _run(['git'] + list(args), cwd=cwd, input=stdin, timeout=limit,
                            env=child_env(env, args))
    except subprocess.TimeoutExpired:
        return Result(None, b'', 'git %s timed out after %s seconds' % (_short(args), limit))
    except OSError as exc:
        return Result(None, b'', 'could not run git in %s: %s' % (cwd, exc))
    finally:
        cache.child(subcommand(args))
    return Result(rc, out or b'', (err or b'').decode('utf-8', 'replace').strip())


def _run(argv, cwd=None, input=None, timeout=None, env=None):
    """`(rc, stdout, stderr)` bytes of argv run in its own job. Raises OSError when it cannot start
    (kitlib.proc.job.JobError when it cannot be put in a job: nothing runs outside one), and
    subprocess.TimeoutExpired when it has not finished within `timeout` seconds (None: no limit);
    the job's whole tree has then been ended.

    The exchange runs on its own thread (input is written there too, so a git that stops reading
    cannot block the caller), and the caller waits for it with the deadline. After the tree is
    ended the pipes close and the thread finishes; if something outside the job still holds a pipe
    the thread is left to it after KILL_GRACE and the call returns anyway."""
    job = _job.Job(kill_on_close=False, short=True)
    try:
        p = job.start(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      stdin=subprocess.PIPE if input is not None else None)
        if timeout is None:
            out, err = p.communicate(input)
            return p.returncode, out, err
        box = {}

        def talk():
            try:
                box['out'] = p.communicate(input)
            except BaseException as exc:          # handed to the caller below
                box['exc'] = exc
        t = threading.Thread(target=talk, name='kit-git-call', daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            job.terminate()
            t.join(KILL_GRACE)
            try:
                p.wait(KILL_GRACE)
            except subprocess.TimeoutExpired:
                pass
            raise subprocess.TimeoutExpired(argv, timeout)
        if 'exc' in box:
            raise box['exc']
        out, err = box['out']
        return p.returncode, out, err
    finally:
        job.close()


_REAL_RUN = _run


def _fake_call(args, cwd, stdin, env):
    """call()'s answer from the fake git: the same Result shape, a missing cwd the same refusal."""
    from kitlib.git import fake
    if cwd is not None and not os.path.isdir(cwd):
        return Result(None, b'', 'could not run git in %s: %s' % (cwd, fake.missing_cwd_error()))
    rc, out, err = fake.run(list(args), cwd, stdin, env=child_env(env, args))
    return Result(rc, out, err.strip())


def spawner():
    """The function call() hands its git argv to now, with `_run`'s shape: `_run` itself, or under
    fake_active() (with `_run` not swapped) the fake's stand-in. For a test spy that wraps it to
    count call()'s git children and passes each on."""
    if _run is _REAL_RUN and fake_active():
        return _fake_run
    return _run


def _fake_run(argv, cwd=None, input=None, timeout=None, env=None):
    """`_run`'s `(rc, stdout, stderr)` bytes for a git argv, answered by the fake."""
    from kitlib.git import fake
    if cwd is not None and not os.path.isdir(cwd):
        raise fake.missing_cwd_error()
    rc, out, err = fake.run([os.fspath(a) for a in argv[1:]], cwd, input, env=env)
    return rc, out, err.encode('utf-8')


def subprocess_run(argv, **kw):
    """`subprocess.run(argv, **kw)`, for kit code that starts git itself instead of through call().
    Production gets subprocess.run through kitlib.proc.spawn.run: the same call from a parent with
    a console, CREATE_NO_WINDOW added from one without, unless `kw` makes its own console choice.
    Under fake_active() an argv whose program is git is answered by the fake as a CompletedProcess
    (kitlib.git.fake.completed) and nothing starts."""
    if fake_active() and _is_git(argv):
        from kitlib.git import fake
        return fake.completed(argv, **kw)
    return _spawn.run(argv, **kw)


def _is_git(argv):
    if isinstance(argv, (str, bytes)) or not argv:
        return False
    prog = os.path.basename(os.fspath(argv[0])).lower()
    return prog in ('git', 'git.exe')


def run(args, cwd, stdin=None, timeout=_DEFAULT, env=None):
    """`(stdout_bytes, None)` when git exits 0, else `(None, why)`."""
    r = call(args, cwd, stdin=stdin, timeout=timeout, env=env)
    if r.rc is None:
        return None, r.err
    if r.rc != 0:
        return None, r.err or 'git %s exited %d' % (_short(args), r.rc)
    return r.out, None


def run_text(args, cwd, timeout=_DEFAULT, env=None, strip=True):
    """`(stdout_text, None)` or `(None, why)`; text as `text()` decodes it. `strip=False` keeps
    leading spaces (porcelain status lines start with one)."""
    out, why = run(args, cwd, timeout=timeout, env=env)
    if out is None:
        return None, why
    t = text(out)
    return (t.strip() if strip else t), None


def text(data):
    """Bytes from git as text: UTF-8 with errors replaced, CRLF and lone CR read as LF."""
    return data.decode('utf-8', errors='replace').replace('\r\n', '\n').replace('\r', '\n')


def _short(args):
    return ' '.join(str(a) for a in list(args)[:3])


def genuine():
    """True when call() and _run() are this module's own and real git answers: a test that
    replaces either is faking git's answers, and so is the fake git (fake_active), whose writes
    do not pass cache.child; kitlib.git.cache keeps none of them (and hands back none it kept)."""
    return call is _CALL and _run is _RUN and not fake_active()


_CALL, _RUN = call, _run
