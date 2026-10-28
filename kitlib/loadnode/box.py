"""The load node's side of `node.py run` (docs/load-nodes.md): its mirrors, its jobs and the run
verbs its forced command (optional/loadnode/loadnode.py) serves besides `status`.

Everything lives under one folder, the base (default `~/pmkit-node` of the node account):

    mirrors/<repo>.git          a bare mirror per repository, made on the first push to it
    hooks-empty/                every mirror's core.hooksPath: a push runs no hook
    jobs.lock                   held (flock) while a job is counted and started
    jobs/<job>/spec.json        what main asked for (check_spec)
    jobs/<job>/pid.json         the job runner's pid, written by start
    jobs/<job>/run.json         its machine lock run id, written by the runner
    jobs/<job>/log              the run's output and the lock's own lines
    jobs/<job>/stop.json        main asked it to stop
    jobs/<job>/result.json      {exit, how, rid, sha, ended}: the job has ended
    jobs/<job>/wt/              the detached worktree at the pushed commit

The words (SSH_ORIGINAL_COMMAND, matched exactly; a verb's parameters are one JSON line on stdin,
at most SPEC_CAP bytes, checked here before anything is done):

    git-receive-pack 'pmkit-node/mirrors/<repo>.git'   the push of the job's commit to
                                 refs/node/<job>; the mirror is made when missing, then git's
                                 receive-pack runs on it (nothing else of git is served)
    start  {spec}                count the live jobs under jobs.lock (refused, busy, at the spec's
                                 max_jobs), check the ref is the sha, make the worktree, and start
                                 the job runner detached (kitlib.lock.detach); old ended jobs are
                                 swept first
    follow {job, offset}         frames (kitlib.loadnode.frames): L log bytes from offset, K while
                                 nothing is new, then R the result and E
    fetch  {job}                 M the manifest of the spec's artifact globs (regular files inside
                                 the worktree, no link on the way), F their bytes, E
    clean  {job}                 remove an ended job: worktree, ref and folder
    stop   {job}                 ask the job's machine lock run to stop (kitlib.lock.run
                                 request_stop), else end the runner with SIGTERM

The job runner (`loadnode.py job <job>`, started only by `start`, never over ssh) runs the spec's
command through kitlib.lock.run.run_command under the node's own machine lock, its output to the
log, and writes result.json. A refusal is one JSON line {"ok": false, "error": ...} (framed verbs:
an X frame); the error never repeats what was sent. Nothing here prints to a user.
"""
import contextlib
import datetime
import glob
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import sys
import time

import kitlib.platform
from kitlib.loadnode import frames
from kitlib.loadnode import registry

JOB_RE = re.compile(r'j-\d{8}T\d{6}Z-[0-9a-f]{6}')
SHA_RE = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
KIND_RE = re.compile(r'[a-z0-9][a-z0-9_-]{0,39}')
RECEIVE_RE = re.compile(r"git-receive-pack 'pmkit-node/mirrors/(%s)\.git'" % registry.REPO_RE)
VERBS = ('status', 'start', 'follow', 'fetch', 'clean', 'stop')
RUN_VERBS = VERBS[1:]

SPEC_CAP = 64 * 1024            # bytes of one parameters line
MAX_ARGS, ARG_CAP = 256, 8192
MAX_GLOBS, GLOB_CAP = 32, 200
NOTE_CAP = 200
MAX_WAIT_S = 7 * 86400
MAX_FILES = 1000
MAX_BYTES = 4 * 1024 ** 3       # every artifact of one job together
CHUNK = 64 * 1024
SWEEP_S = 7 * 86400             # an ended job's folder is removed at a start this long after
POLL_S = 0.5
KEEP_S = 15.0                   # follow sends K when nothing new came for this long

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2


class SpecError(Exception):
    """Parameters that are not what a verb takes. The message never quotes them."""


def new_job_id(now=None):
    """'j-20261028T120000Z-abcdef'."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return 'j-%s-%s' % (now.strftime('%Y%m%dT%H%M%SZ'), secrets.token_hex(3))


def default_base():
    return os.path.join(os.path.expanduser('~'), 'pmkit-node')


def _iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# -- parameters ----------------------------------------------------------------------------------

def read_params(stream, cap=SPEC_CAP):
    """The one JSON object line on `stream` (binary). SpecError when it is longer than `cap`,
    not UTF-8 JSON, not an object or more than one line."""
    data = stream.read(cap + 1)
    if len(data) > cap:
        raise SpecError('the parameters are longer than %d bytes' % cap)
    lines = [x for x in data.split(b'\n') if x.strip()]
    if len(lines) != 1:
        raise SpecError('the parameters are not one line')
    try:
        obj = json.loads(lines[0].decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise SpecError('the parameters are not JSON')
    if not isinstance(obj, dict):
        raise SpecError('the parameters are not an object')
    return obj


def _job(v):
    if not isinstance(v, str) or not JOB_RE.fullmatch(v):
        raise SpecError('job is not a job id')
    return v


def glob_ok(g):
    """True for an artifact glob main may ask for: relative to the checkout's top, '/' between
    parts, no '..', no '.git' part, nothing absolute, no drive, no control character."""
    if not isinstance(g, str) or not g or len(g) > GLOB_CAP:
        return False
    if g.startswith(('/', '\\')) or ':' in g or '\\' in g or re.search(r'[\x00-\x1f\x7f]', g):
        return False
    parts = g.split('/')
    return all(p and p not in ('.', '..', '.git') for p in parts)


def check_spec(d):
    """The checked start spec in `d` (a dict): a new dict of the known keys. SpecError naming
    the first key that is wrong."""
    out = {'job': _job(d.get('job'))}
    if not registry.repo_ok(d.get('repo')):
        raise SpecError('repo is not a repo name')
    if not registry.repo_ok(d.get('slug')):
        raise SpecError('slug is not a slug')
    if not isinstance(d.get('kind'), str) or not KIND_RE.fullmatch(d['kind']):
        raise SpecError('kind is not a kind name')
    if not isinstance(d.get('sha'), str) or not SHA_RE.fullmatch(d['sha']):
        raise SpecError('sha is not a full commit id')
    cmd = d.get('cmd')
    if (not isinstance(cmd, list) or not cmd or len(cmd) > MAX_ARGS
            or not all(isinstance(a, str) and len(a) <= ARG_CAP and '\x00' not in a for a in cmd)
            or not cmd[0]):
        raise SpecError('cmd is not a list of 1 to %d arguments' % MAX_ARGS)
    arts = d.get('artifacts', [])
    if not isinstance(arts, list) or len(arts) > MAX_GLOBS or not all(glob_ok(g) for g in arts):
        raise SpecError('artifacts is not a list of at most %d relative globs' % MAX_GLOBS)
    mj = d.get('max_jobs')
    if type(mj) is not int or not 1 <= mj <= 1024:
        raise SpecError('max_jobs is not a whole number from 1 to 1024')
    for k in ('wait_s', 'expect_s'):
        v = d.get(k)
        if v is not None and (type(v) not in (int, float) or not 0 <= v <= MAX_WAIT_S):
            raise SpecError('%s is not a number of seconds' % k)
    if not isinstance(d.get('no_wait', False), bool):
        raise SpecError('no_wait is not true or false')
    note = d.get('note')
    if note is not None and (not isinstance(note, str) or len(note) > NOTE_CAP):
        raise SpecError('note is not a short string')
    out.update(repo=d['repo'], slug=d['slug'], kind=d['kind'], sha=d['sha'], cmd=list(cmd),
               artifacts=list(arts), max_jobs=mj, wait_s=d.get('wait_s'),
               expect_s=d.get('expect_s'), no_wait=d.get('no_wait', False), note=note)
    return out


# -- the box -------------------------------------------------------------------------------------

def _git_default(args, cwd=None):
    from kitlib.git import cmd as gitcmd
    r = gitcmd.call(args, cwd=cwd or os.getcwd(), timeout=600)
    return r.rc, r.out, r.err


def _alive_default(pid, job):
    """True while `pid` is the runner of `job`: it exists and (where /proc says) its command
    line names the job, so a reused pid is not taken for it. Never signals on Windows."""
    if kitlib.platform.NAME != 'linux':
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass
    except OSError:
        return False
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            return job.encode() in f.read()
    except OSError:
        return True


def _detach_default(argv, cwd, env):
    from kitlib.lock import detach
    return detach.create(argv, cwd, env)


@contextlib.contextmanager
def _flock(path):
    """Hold an exclusive lock on the file `path` (fcntl.flock on the node; msvcrt where the
    tests run)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, 'a+b')
    try:
        try:
            import fcntl
        except ImportError:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            return
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    tmp = '%s.%d.tmp' % (path, os.getpid())
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f)
    os.replace(tmp, path)


class Box(object):
    """One node's jobs under `base`. git(args, cwd) -> (rc, out bytes, err text), alive(pid, job),
    detach(argv, cwd, env) -> (pid, why) and clock() are the tests' stand-ins. `runner` is the
    argv head that runs `loadnode.py` (the job runner is `runner + ['job', <job>]`)."""

    def __init__(self, base=None, runner=None, git=None, alive=None, detach=None, clock=time.time):
        self.base = os.path.abspath(base or default_base())
        self.runner = list(runner or [])
        self.git = git or _git_default
        self.alive = alive or _alive_default
        self.detach = detach or _detach_default
        self.clock = clock

    # paths
    def mirror(self, repo):
        return os.path.join(self.base, 'mirrors', repo + '.git')

    def job_dir(self, job):
        return os.path.join(self.base, 'jobs', job)

    def _p(self, job, name):
        return os.path.join(self.job_dir(job), name)

    # state
    def is_live(self, job):
        """True while `job` has no result and its runner is alive."""
        if os.path.exists(self._p(job, 'result.json')):
            return False
        pid = (_read_json(self._p(job, 'pid.json')) or {}).get('pid')
        return type(pid) is int and pid > 0 and bool(self.alive(pid, job))

    def jobs(self):
        try:
            names = os.listdir(os.path.join(self.base, 'jobs'))
        except OSError:
            return []
        return sorted(n for n in names if JOB_RE.fullmatch(n))

    def live_count(self):
        return sum(1 for j in self.jobs() if self.is_live(j))

    # git-receive-pack
    def mirror_for_push(self, repo):
        """The mirror of `repo`, made (bare, no hooks, objects checked) when missing."""
        m = self.mirror(repo)
        if not os.path.isdir(m):
            hooks = os.path.join(self.base, 'hooks-empty')
            os.makedirs(hooks, exist_ok=True)
            os.makedirs(os.path.dirname(m), exist_ok=True)
            for args in (['init', '--bare', '--quiet', m],
                         ['--git-dir', m, 'config', 'core.hooksPath', hooks],
                         ['--git-dir', m, 'config', 'receive.fsckObjects', 'true'],
                         ['--git-dir', m, 'config', 'receive.denyDeletes', 'true'],
                         ['--git-dir', m, 'config', 'receive.denyNonFastForwards', 'true']):
                rc, _o, err = self.git(args, self.base)
                if rc:
                    raise OSError('git %s failed: %s' % (args[0] if args[0] != '--git-dir' else args[2],
                                                         (err or '').strip()[-200:]))
        return m

    # start
    def start(self, spec, env=None):
        """The reply to `start` for the checked spec (check_spec)."""
        job, m = spec['job'], self.mirror(spec['repo'])
        if not os.path.isdir(m):
            return {'ok': False, 'error': 'no mirror of %s here: push first' % spec['repo']}
        rc, out, _err = self.git(['--git-dir', m, 'rev-parse', '--verify', '--quiet',
                                  'refs/node/%s^{commit}' % job], self.base)
        if rc or out.decode('ascii', 'replace').strip() != spec['sha']:
            return {'ok': False, 'error': 'refs/node/%s is not the commit asked for' % job}
        with _flock(os.path.join(self.base, 'jobs.lock')):
            self.sweep()
            live = self.live_count()
            if live >= spec['max_jobs']:
                return {'ok': False, 'busy': True, 'jobs': live,
                        'error': 'the node runs %d job%s already, its most' % (live, '' if live == 1 else 's')}
            d = self.job_dir(job)
            if os.path.lexists(d):
                return {'ok': False, 'error': 'job %s exists already' % job}
            os.makedirs(d)
            _write_json(self._p(job, 'spec.json'), spec)
            wt = self._p(job, 'wt')
            rc, _o, err = self.git(['--git-dir', m, 'worktree', 'add', '--detach', '--quiet', wt,
                                    spec['sha']], self.base)
            if rc:
                self._result(job, None, 'not started', spec['sha'], 'git worktree add failed')
                return {'ok': False, 'error': 'the worktree could not be made: %s' % (err or '').strip()[-200:]}
            argv = self.runner + ['job', job]
            if os.path.normcase(self.base) != os.path.normcase(os.path.abspath(default_base())):
                argv += ['--base', self.base]
            pid, why = self.detach(argv, wt, runner_env(os.environ if env is None else env))
            if not pid:
                self._result(job, None, 'not started', spec['sha'], why)
                return {'ok': False, 'error': 'the job could not start: %s' % why}
            _write_json(self._p(job, 'pid.json'), {'pid': pid, 'at': _iso()})
        return {'ok': True, 'job': job, 'pid': pid}

    def _result(self, job, code, how, sha, why=None):
        _write_json(self._p(job, 'result.json'), {'exit': code, 'how': how, 'sha': sha,
                                                  'why': why, 'ended': _iso()})

    def sweep(self, age_s=SWEEP_S):
        """Remove jobs that ended (or whose runner is gone) longer than `age_s` ago."""
        now = self.clock()
        for job in self.jobs():
            res = self._p(job, 'result.json')
            try:
                at = os.path.getmtime(res if os.path.exists(res) else self.job_dir(job))
            except OSError:
                continue
            if now - at > age_s and not self.is_live(job):
                self.clean(job)

    # follow
    def follow(self, job, offset, write, sleep=time.sleep, clock=time.monotonic, poll_s=POLL_S,
               keep_s=KEEP_S):
        """Stream `job`'s log from byte `offset` as frames until its result: write(bytes)."""
        if not os.path.isdir(self.job_dir(job)):
            write(frames.encode(frames.ERROR, json.dumps({'error': 'no job %s here' % job}).encode()))
            return EXIT_REFUSED
        log = self._p(job, 'log')
        pos = max(0, offset)
        try:
            pos = min(pos, os.path.getsize(log))
        except OSError:
            pos = 0
        last = clock()
        while True:
            chunk = b''
            try:
                with open(log, 'rb') as f:
                    f.seek(pos)
                    chunk = f.read(CHUNK)
            except OSError:
                pass
            if chunk:
                write(frames.encode(frames.LOG, chunk))
                pos += len(chunk)
                last = clock()
                continue
            res = _read_json(self._p(job, 'result.json'))
            if res is None and not self.is_live(job):
                res = _read_json(self._p(job, 'result.json'))   # it may have ended just now
                if res is None:
                    res = {'exit': None, 'how': 'vanished', 'why': 'the job runner is gone'}
                else:
                    continue                                    # read its last log bytes first
            if res is not None:
                res['offset'] = pos
                write(frames.encode(frames.RESULT, json.dumps(res).encode()))
                write(frames.encode(frames.END))
                return EXIT_OK
            if clock() - last >= keep_s:
                write(frames.encode(frames.KEEP))
                last = clock()
            sleep(poll_s)

    # fetch
    def artifacts(self, job):
        """[(relative path, full path)] of the job's artifact files: matches of the spec's globs
        that are regular files inside its worktree with no link on the way and no .git part."""
        spec = _read_json(self._p(job, 'spec.json')) or {}
        wt = self._p(job, 'wt')
        real = os.path.realpath(wt)
        found = {}
        for g in spec.get('artifacts') or []:
            if not glob_ok(g):
                continue
            for rel in glob.glob(g, root_dir=wt, recursive=True):
                parts = rel.replace(os.sep, '/').split('/')
                if any(p in ('', '.', '..', '.git') for p in parts):
                    continue
                cur, ok = wt, True
                for p in parts:
                    cur = os.path.join(cur, p)
                    if os.path.islink(cur):
                        ok = False
                        break
                try:
                    st = os.lstat(cur)
                except OSError:
                    continue
                rp = os.path.realpath(cur)
                if ok and stat.S_ISREG(st.st_mode) and rp.startswith(real + os.sep):
                    found['/'.join(parts)] = cur
        return sorted(found.items())

    def fetch(self, job, write):
        """M the manifest {files: [{path, size, sha256}], error}, F the files' bytes, E."""
        if not os.path.isdir(self.job_dir(job)):
            write(frames.encode(frames.ERROR, json.dumps({'error': 'no job %s here' % job}).encode()))
            return EXIT_REFUSED
        files, total, error = [], 0, None
        for rel, full in self.artifacts(job):
            h, size = hashlib.sha256(), 0
            with open(full, 'rb') as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(chunk)
                    size += len(chunk)
            files.append({'path': rel, 'size': size, 'sha256': h.hexdigest(), 'full': full})
            total += size
            if len(files) > MAX_FILES or total > MAX_BYTES:
                files, error = [], 'the artifacts are over %d files or %d bytes' % (MAX_FILES, MAX_BYTES)
                break
        write(frames.encode(frames.MANIFEST, json.dumps(
            {'files': [{k: f[k] for k in ('path', 'size', 'sha256')} for f in files],
             'error': error}).encode()))
        for f in files:
            left = f['size']
            with open(f['full'], 'rb') as fh:     # exactly size bytes: a changed file is padded or cut
                while left:
                    chunk = fh.read(min(CHUNK * 16, left)) or b'\0' * min(CHUNK * 16, left)
                    write(frames.encode(frames.FILE, chunk))
                    left -= len(chunk)
        write(frames.encode(frames.END))
        return EXIT_OK

    # clean
    def clean(self, job):
        """Remove an ended job: its worktree, its ref and its folder."""
        d = self.job_dir(job)
        if not os.path.isdir(d):
            return {'ok': True, 'gone': True}
        if self.is_live(job):
            return {'ok': False, 'error': 'job %s is still running: stop it first' % job}
        spec = _read_json(self._p(job, 'spec.json')) or {}
        m = self.mirror(spec['repo']) if registry.repo_ok(spec.get('repo')) else None
        if m and os.path.isdir(m):
            self.git(['--git-dir', m, 'worktree', 'remove', '--force', self._p(job, 'wt')], self.base)
        shutil.rmtree(d, ignore_errors=True)
        if m and os.path.isdir(m):
            self.git(['--git-dir', m, 'worktree', 'prune'], self.base)
            self.git(['--git-dir', m, 'update-ref', '-d', 'refs/node/' + job], self.base)
        return {'ok': True, 'gone': not os.path.exists(d)}

    # stop
    def stop(self, job, request_stop=None, kill=None):
        """Ask `job`'s machine lock run to stop; when it has none yet, SIGTERM its runner."""
        if not os.path.isdir(self.job_dir(job)):
            return {'ok': False, 'error': 'no job %s here' % job}
        if not self.is_live(job):
            return {'ok': True, 'ended': True}
        _write_json(self._p(job, 'stop.json'), {'at': _iso()})
        rid = (_read_json(self._p(job, 'run.json')) or {}).get('rid')
        if rid:
            if request_stop is None:
                from kitlib.lock import run as lockrun
                from kitlib.lock.store import Store
                ok = lockrun.request_stop(Store(), rid, 'node.py stop from the main machine')
            else:
                ok = request_stop(rid)
            if ok:
                return {'ok': True, 'asked': 'its machine lock run %s' % rid}
        pid = (_read_json(self._p(job, 'pid.json')) or {}).get('pid')
        (kill or os.kill)(pid, getattr(signal, 'SIGTERM', 15))
        return {'ok': True, 'asked': 'its runner, by SIGTERM'}

    # the job runner
    def run_job(self, job, run_command=None, new_rid=None, store_factory=None, chdir=os.chdir):
        """Run `job`'s command under the machine lock: the exit code it ended with. Its log is
        the job's `log` (the caller has pointed fds 1 and 2 at it)."""
        spec = _read_json(self._p(job, 'spec.json'))
        if spec is None:
            return EXIT_USAGE
        log = self._p(job, 'log')

        def say(text):
            with open(log, 'a', encoding='utf-8', newline='\n') as f:
                f.write(text + '\n')
        if run_command is None:
            from kitlib.lock import run as lockrun
            run_command = lockrun.run_command
        if new_rid is None:
            from kitlib.lock.store import new_id
            new_rid = lambda: new_id('r')   # noqa: E731
        if store_factory is None:
            from kitlib.lock.store import Store
            store_factory = Store
        from kitlib.lock.store import LockError, EXIT_BUSY
        rid = new_rid()
        _write_json(self._p(job, 'run.json'), {'rid': rid})
        chdir(self._p(job, 'wt'))
        how, why = 'ended', None
        try:
            code = run_command(spec['cmd'], spec['kind'], spec['repo'], spec['slug'],
                               store=store_factory(say=say), rid=rid, wait_s=spec.get('wait_s'),
                               no_wait=spec.get('no_wait', False), expect_s=spec.get('expect_s'),
                               note=spec.get('note'), env=runner_env(os.environ, job))
        except LockError as e:
            code, how, why = e.code, 'busy' if e.code == EXIT_BUSY else 'refused', e.message
            say(e.message)
        except KeyboardInterrupt:
            code, how = 130, 'interrupted'
        except Exception as e:                  # the runner must always leave a result
            code, how, why = 1, 'error', '%s: %s' % (type(e).__name__, e)
            say('the job runner failed: ' + why)
        if how in ('ended', 'interrupted') and os.path.exists(self._p(job, 'stop.json')):
            how = 'stopped'
        self._result(job, code, how, spec['sha'], why)
        return code if isinstance(code, int) else 1


def runner_env(env, job=None):
    """`env` without the machine lock's own variables (the job is no nested or detached run of
    anything on the node) and without sshd's SSH_ORIGINAL_COMMAND (loadnode.py is the job runner
    only when that is not set), with PMKIT_NODE_JOB set when `job` is given."""
    out = {k: v for k, v in env.items()
           if not k.startswith('MACHINE_LOCK_') and k != 'SSH_ORIGINAL_COMMAND'}
    if job:
        out['PMKIT_NODE_JOB'] = job
    return out


# -- serving -------------------------------------------------------------------------------------

def _reply(write, d):
    write((json.dumps(d, separators=(',', ':'), ensure_ascii=True) + '\n').encode('ascii'))


def serve(cmd, stdin, write, box, exec_fn=None):
    """Serve the run word `cmd` (SSH_ORIGINAL_COMMAND, already known not to be `status`):
    the exit code. stdin is binary; write(bytes) sends to main. exec_fn(argv) replaces this
    process (os.execvp) for git's receive-pack."""
    m = RECEIVE_RE.fullmatch(cmd)
    if m and registry.repo_ok(m.group(1)):
        try:
            path = box.mirror_for_push(m.group(1))
        except OSError as e:
            _reply(write, {'ok': False, 'error': 'the mirror could not be made (%s)' % type(e).__name__})
            return EXIT_REFUSED
        (exec_fn or (lambda argv: os.execvp(argv[0], argv)))(['git', 'receive-pack', path])
        return EXIT_OK
    if cmd not in RUN_VERBS:
        return None
    framed = cmd in ('follow', 'fetch')
    try:
        p = read_params(stdin)
        if cmd == 'start':
            p = check_spec(p)
        else:
            p = {'job': _job(p.get('job')), 'offset': p.get('offset', 0)}
            if type(p['offset']) is not int or p['offset'] < 0:
                raise SpecError('offset is not a byte count')
    except SpecError as e:
        if framed:
            write(frames.encode(frames.ERROR, json.dumps({'error': str(e)}).encode()))
        else:
            _reply(write, {'ok': False, 'error': str(e)})
        return EXIT_USAGE
    if cmd == 'follow':
        return box.follow(p['job'], p['offset'], write)
    if cmd == 'fetch':
        return box.fetch(p['job'], write)
    d = getattr(box, cmd)(p if cmd == 'start' else p['job'])      # start, clean or stop
    _reply(write, d)
    return EXIT_OK if d.get('ok') else EXIT_REFUSED


def job_main(job, base, runner=None):
    """`loadnode.py job <job>`: the detached runner. Points fds 1 and 2 at the job's log, turns
    SIGTERM into Ctrl+C (so the run is released and a result written), and runs it."""
    box = Box(base, runner=runner)
    if not JOB_RE.fullmatch(job) or not os.path.isdir(box.job_dir(job)):
        return EXIT_USAGE
    fd = os.open(box._p(job, 'log'), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    sys.stdout = os.fdopen(1, 'w', buffering=1, encoding='utf-8', errors='replace', closefd=False)
    sys.stderr = os.fdopen(2, 'w', buffering=1, encoding='utf-8', errors='replace', closefd=False)

    def term(_sig, _frame):
        raise KeyboardInterrupt()
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, term)
    return box.run_job(job)
