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
    jobs/<job>/repo.git/        a gate job's own bare repository: the mirror's objects (its
                                alternates), the job's base as <remote>/<main> and <main>, and
                                gate/<repo>/ its linked worktree, so no two gates share a ref
    jobs/<job>/gate/<repo>/     a gate job's worktree at the pushed commit, named for the
                                repository as the gate names its record's repo by its folder

The words (SSH_ORIGINAL_COMMAND, matched exactly; a verb's parameters are one JSON line on stdin,
at most SPEC_CAP bytes, checked here before anything is done):

    git-receive-pack 'pmkit-node/mirrors/<repo>.git'   the push of the job's commit to
                                 refs/node/<job>; the mirror is made when missing, then git's
                                 receive-pack runs on it (nothing else of git is served)
    start  {spec}                count the live jobs under jobs.lock (refused, busy, at the spec's
                                 max_jobs), check the ref is the sha, make the worktree, and start
                                 the job runner detached (kitlib.lock.detach); old ended jobs are
                                 swept first; refused before the job's folder is made, it deletes
                                 the pushed ref
    gate   {gate spec}           start a gate job as start does (check_gate_spec): the pushed ref
                                 is the sha and refs/node/<job>-base the base; gate/<repo>/ is a
                                 linked worktree of the job's own repo.git; the runner runs the
                                 node kit's own `tools/gate.py -C <it> --full` (no command main
                                 sends), which books the node's machine lock itself, then puts
                                 the gate-OK record it wrote for the sha (gate_ok.verify_full) in
                                 result.json
    follow {job, offset}         frames (kitlib.loadnode.frames): L log bytes from offset, K while
                                 nothing is new, then R the result and E
    fetch  {job}                 M the manifest of the spec's artifact globs (regular files inside
                                 the worktree, no link on the way), F their bytes, E
    clean  {job}                 remove an ended job: worktree, refs and folder
    stop   {job}                 ask the job's machine lock run to stop (kitlib.lock.run
                                 request_stop), else end the runner with SIGTERM

The job runner (`loadnode.py job <job>`, started only by `start` and `gate`, never over ssh) runs
the spec's command through kitlib.lock.run.run_command under the node's own machine lock (a gate
job: the node's gate.py, run_gate), its output to the log, and writes result.json. A start sweeps
old ended jobs, and the refs/node/ refs no job folder owns (a push whose start never came) once
the time in their job id is SWEEP_S old. A refusal is one JSON line {"ok": false, "error": ...}
(framed verbs: an X frame); the error never repeats what was sent. Nothing here prints to a user.
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
GATE_VERB = 'gate'
RUN_VERBS = ('start', 'follow', 'fetch', 'clean', 'stop')     # what node.py run needs of a node
VERBS = ('status', 'start', GATE_VERB, 'follow', 'fetch', 'clean', 'stop')
REMOTE_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,39}')
BRANCH_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,99}){0,4}')
BASE_SUFFIX = '-base'           # refs/node/<job>-base: a gate job's base commit
KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GATE_STOP_S = 60                # a stopped gate gets this long after its Ctrl+C before a kill

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


GLOB_RULE = 'ASCII, no "..", no ".git", "/" between parts'      # glob_ok, for a usage line


def glob_ok(g):
    """True for an artifact glob main may ask for: relative to the checkout's top, '/' between
    parts, no '..', no '.git' part, nothing absolute, no drive, no control character, ASCII only
    (as the artifact paths: job.artifact_path_ok)."""
    if not isinstance(g, str) or not g or len(g) > GLOB_CAP or not g.isascii():
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


def branch_ok(name):
    """True for a main branch name a gate spec may carry: parts of letters, digits, '.', '_' and
    '-' with '/' between, no '..' and no '.lock' end (git would refuse those)."""
    return (isinstance(name, str) and bool(BRANCH_RE.fullmatch(name)) and '..' not in name
            and not name.endswith('.lock'))


def check_gate_spec(d):
    """The checked gate spec in `d` (a dict): a new dict of the known keys, with `gate` True.
    There is no command: the node runs its own gate.py. SpecError naming the first wrong key."""
    out = {'job': _job(d.get('job'))}
    if not registry.repo_ok(d.get('repo')):
        raise SpecError('repo is not a repo name')
    if not registry.repo_ok(d.get('slug')):
        raise SpecError('slug is not a slug')
    for k in ('sha', 'base'):
        if not isinstance(d.get(k), str) or not SHA_RE.fullmatch(d[k]):
            raise SpecError('%s is not a full commit id' % k)
    remote = d.get('remote')
    if not isinstance(remote, str) or (remote and not REMOTE_RE.fullmatch(remote)):
        raise SpecError('remote is not a remote name')
    if not branch_ok(d.get('main')):
        raise SpecError('main is not a branch name')
    mj = d.get('max_jobs')
    if type(mj) is not int or not 1 <= mj <= 1024:
        raise SpecError('max_jobs is not a whole number from 1 to 1024')
    note = d.get('note')
    if note is not None and (not isinstance(note, str) or len(note) > NOTE_CAP):
        raise SpecError('note is not a short string')
    out.update(repo=d['repo'], slug=d['slug'], sha=d['sha'], base=d['base'], remote=remote,
               main=d['main'], max_jobs=mj, note=note, gate=True)
    return out


def _job_time(job):
    """The epoch seconds of the UTC time in a job id (new_job_id), or None."""
    try:
        return datetime.datetime.strptime(job[2:17], '%Y%m%dT%H%M%S').replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


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

    def tree(self, spec):
        """The job's worktree: wt/, or a gate job's gate/<repo>/ (the gate takes its record's repo
        name from the folder when the repository's config names none)."""
        if spec.get('gate') is True:
            return os.path.join(self.job_dir(spec['job']), 'gate', spec['repo'])
        return self._p(spec['job'], 'wt')

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
        return self._begin(spec, env, self._worktree)

    def gate(self, spec, env=None):
        """The reply to `gate` for the checked gate spec (check_gate_spec): start's, with the
        base ref checked too and the worktree a linked one of the job's own repo.git."""
        return self._begin(spec, env, self._gate_tree)

    def _rev(self, m, ref):
        """The commit `ref` names in the repository `m`, or None."""
        rc, out, _err = self.git(['--git-dir', m, 'rev-parse', '--verify', '--quiet',
                                  '%s^{commit}' % ref], self.base)
        return None if rc else out.decode('ascii', 'replace').strip()

    def _worktree(self, spec, m, wt):
        """Make `wt`, the mirror's detached worktree at the spec's sha: (rc, git's error)."""
        rc, _o, err = self.git(['--git-dir', m, 'worktree', 'add', '--detach', '--quiet', wt,
                                spec['sha']], self.base)
        return rc, err

    def _gate_tree(self, spec, m, wt):
        """Make the job's own bare repo.git (the mirror's objects through its alternates, no
        hooks, the base as refs/remotes/<remote>/<main> and refs/heads/<main>, so the gate's
        default base is the one main sent) and `wt` its linked worktree at the sha: (rc, why)."""
        r = self._p(spec['job'], 'repo.git')
        rc, _o, err = self.git(['init', '--bare', '--quiet', r], self.base)
        if rc:
            return rc, err
        try:
            info = os.path.join(r, 'objects', 'info')
            os.makedirs(info, exist_ok=True)
            with open(os.path.join(info, 'alternates'), 'w', encoding='utf-8', newline='\n') as f:
                f.write(os.path.join(os.path.abspath(m), 'objects') + '\n')
        except OSError as e:
            return 1, 'its alternates could not be written (%s)' % type(e).__name__
        refs = ['refs/heads/' + spec['main']]
        if spec['remote']:
            refs.append('refs/remotes/%s/%s' % (spec['remote'], spec['main']))
        steps = [['--git-dir', r, 'config', 'core.hooksPath',
                  os.path.join(self.base, 'hooks-empty')]]
        steps += [['--git-dir', r, 'update-ref', ref, spec['base']] for ref in refs]
        steps.append(['--git-dir', r, 'worktree', 'add', '--detach', '--quiet', wt, spec['sha']])
        for args in steps:
            rc, _o, err = self.git(args, self.base)
            if rc:
                return rc, err
        return 0, ''

    def _begin(self, spec, env, make_tree):
        """start and gate: check the pushed refs, count the live jobs under jobs.lock, make the
        job's folder and worktree (make_tree) and start its runner detached."""
        job, m = spec['job'], self.mirror(spec['repo'])
        if not os.path.isdir(m):
            return {'ok': False, 'error': 'no mirror of %s here: push first' % spec['repo']}
        wrong = None
        if self._rev(m, 'refs/node/' + job) != spec['sha']:
            wrong = 'refs/node/%s is not the commit asked for' % job
        elif spec.get('gate') and self._rev(m, 'refs/node/' + job + BASE_SUFFIX) != spec['base']:
            wrong = 'refs/node/%s%s is not the base asked for' % (job, BASE_SUFFIX)
        if wrong:
            with _flock(os.path.join(self.base, 'jobs.lock')):
                self._drop_ref(job, m, spec.get('gate'))
            return {'ok': False, 'error': wrong}
        with _flock(os.path.join(self.base, 'jobs.lock')):
            self.sweep()
            live = self.live_count()
            if live >= spec['max_jobs']:
                self._drop_ref(job, m, spec.get('gate'))
                return {'ok': False, 'busy': True, 'jobs': live,
                        'error': 'the node runs %d job%s already, its most' % (live, '' if live == 1 else 's')}
            d = self.job_dir(job)
            if os.path.lexists(d):
                return {'ok': False, 'error': 'job %s exists already' % job}
            os.makedirs(d)
            _write_json(self._p(job, 'spec.json'), spec)
            wt = self.tree(spec)
            rc, err = make_tree(spec, m, wt)
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

    def _drop_ref(self, job, m, gate=False):
        """Delete refs/node/<job> (and a gate's <job>-base) pushed for a start refused before
        the job's folder was made: clean() goes by folders, so only sweep_refs, a week on, would.
        The caller holds jobs.lock; a job whose folder exists keeps its refs (clean removes
        them)."""
        if not os.path.lexists(self.job_dir(job)):
            self._delete_refs(job, m, gate)

    def _delete_refs(self, job, m, gate=False):
        """Delete refs/node/<job>, and for a gate job refs/node/<job>-base, from the mirror `m`
        (a local update-ref: receive.denyDeletes binds pushes only)."""
        self.git(['--git-dir', m, 'update-ref', '-d', 'refs/node/' + job], self.base)
        if gate:
            self.git(['--git-dir', m, 'update-ref', '-d', 'refs/node/' + job + BASE_SUFFIX],
                     self.base)

    def drop_refused(self, d, gate=False):
        """After check_spec (or check_gate_spec, `gate`) refused the start params `d` (a dict):
        drop the refs main pushed for them, when their job id and repo name are sound, the mirror
        is there and the job has no folder. An OSError on the way is ignored: the refusal is the
        reply."""
        job, repo = d.get('job'), d.get('repo')
        if not (isinstance(job, str) and JOB_RE.fullmatch(job) and registry.repo_ok(repo)):
            return
        m = self.mirror(repo)
        if not os.path.isdir(m):
            return
        try:
            with _flock(os.path.join(self.base, 'jobs.lock')):
                self._drop_ref(job, m, gate)
        except OSError:
            pass

    def _result(self, job, code, how, sha, why=None, extra=None):
        d = {'exit': code, 'how': how, 'sha': sha, 'why': why, 'ended': _iso()}
        d.update(extra or {})
        _write_json(self._p(job, 'result.json'), d)

    def sweep(self, age_s=SWEEP_S):
        """Remove jobs that ended (or whose runner is gone) longer than `age_s` ago, then the
        refs no job folder owns (sweep_refs)."""
        now = self.clock()
        for job in self.jobs():
            res = self._p(job, 'result.json')
            try:
                at = os.path.getmtime(res if os.path.exists(res) else self.job_dir(job))
            except OSError:
                continue
            if now - at > age_s and not self.is_live(job):
                self.clean(job)
        self.sweep_refs(age_s)

    def sweep_refs(self, age_s=SWEEP_S):
        """Delete each mirror's refs/node/<job> (and <job>-base) that no job folder owns once the
        time in its job id is `age_s` old: a push whose start never came (main died, or ssh
        failed before the start went out). sweep() and clean() go by job folders, so nothing
        else would; the age leaves a start still on its way its ref. The caller holds jobs.lock."""
        now = self.clock()
        try:
            names = sorted(os.listdir(os.path.join(self.base, 'mirrors')))
        except OSError:
            return
        for name in names:
            m = os.path.join(self.base, 'mirrors', name)
            if not name.endswith('.git') or not os.path.isdir(m):
                continue
            rc, out, _err = self.git(['--git-dir', m, 'for-each-ref', '--format=%(refname)',
                                      'refs/node/'], self.base)
            if rc:
                continue
            for ref in out.decode('utf-8', 'replace').split():
                job = ref[len('refs/node/'):]
                if job.endswith(BASE_SUFFIX):
                    job = job[:-len(BASE_SUFFIX)]
                if not JOB_RE.fullmatch(job) or os.path.lexists(self.job_dir(job)):
                    continue
                at = _job_time(job)
                if at is not None and now - at > age_s:
                    self.git(['--git-dir', m, 'update-ref', '-d', ref], self.base)

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
        """Remove an ended job: its worktree, its refs and its folder (a gate job's repo.git
        with it)."""
        d = self.job_dir(job)
        if not os.path.isdir(d):
            return {'ok': True, 'gone': True}
        if self.is_live(job):
            return {'ok': False, 'error': 'job %s is still running: stop it first' % job}
        spec = _read_json(self._p(job, 'spec.json')) or {}
        m = self.mirror(spec['repo']) if registry.repo_ok(spec.get('repo')) else None
        gate = spec.get('gate') is True     # its tree is repo.git's, which goes with the folder
        if m and os.path.isdir(m) and not gate:
            self.git(['--git-dir', m, 'worktree', 'remove', '--force', self._p(job, 'wt')], self.base)
        shutil.rmtree(d, ignore_errors=True)
        if m and os.path.isdir(m):
            self.git(['--git-dir', m, 'worktree', 'prune'], self.base)
            self._delete_refs(job, m, gate)
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
    def run_job(self, job, run_command=None, new_rid=None, store_factory=None, chdir=os.chdir,
                verify=None):
        """Run `job`'s command under the machine lock: the exit code it ended with. Its log is
        the job's `log` (the caller has pointed fds 1 and 2 at it). A gate job goes to run_gate,
        with `verify`."""
        spec = _read_json(self._p(job, 'spec.json'))
        if spec is None:
            return EXIT_USAGE
        if spec.get('gate') is True:
            return self.run_gate(job, spec, verify=verify)
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
        chdir(self.tree(spec))
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

    def run_gate(self, job, spec, popen=None, verify=None):
        """Run the node kit's own gate on a gate job's worktree, `<KIT_ROOT>/tools/gate.py -C <tree>
        --full`, never a command main sent; the exit code it ended with. gate.py books the node's
        machine lock itself (runner_env leaves no MACHINE_LOCK_ variable). A Ctrl+C (the runner's
        SIGTERM: stop) is passed on to it as SIGINT, then a kill after GATE_STOP_S. When it
        passes, result.json carries the gate-OK record it wrote for the sha as `record` (verify(wt,
        sha) -> (record, why): the job runner passes kitlib.loadnode.gaterecord.read_record, kept
        out of this module so the lock tool never reaches the record modules), else why not as
        `record_why`. popen is spawn.popen, the tests' stand-in."""
        from kitlib.proc import spawn
        wt = self.tree(spec)
        argv = [sys.executable, '-B', os.path.join(KIT_ROOT, 'tools', 'gate.py'), '-C', wt,
                '--full']
        how, extra = 'ended', {'gate': True, 'record': None, 'record_why': None}
        try:
            p = (popen or spawn.popen)(argv, cwd=wt, env=runner_env(os.environ, job))
        except OSError as e:
            self._result(job, 1, 'error', spec['sha'], 'the gate could not start (%s)' %
                         type(e).__name__, extra)
            return 1
        try:
            code = p.wait()
        except KeyboardInterrupt:
            how = 'interrupted'
            try:
                p.send_signal(getattr(signal, 'SIGINT', 2))
                code = p.wait(GATE_STOP_S)
            except (Exception, KeyboardInterrupt):  # noqa: BLE001 - a second stop too: it ends
                p.kill()
                code = p.wait()
            if code in (None, 0):
                code = 130
        if how == 'interrupted' and os.path.exists(self._p(job, 'stop.json')):
            how = 'stopped'
        if code == 0:
            try:
                if verify is None:
                    raise LookupError('this job runner reads no gate-OK record')
                rec, rwhy = verify(wt, spec['sha'])
            except Exception as e:              # noqa: BLE001 - the result is written anyway
                rec, rwhy = None, '%s: %s' % (type(e).__name__, e)
            if rec is not None and rec.get('commit') != spec['sha']:
                rec, rwhy = None, 'the record names another commit'
            extra.update(record=rec, record_why=None if rec is not None else str(rwhy)[:300])
        self._result(job, code, how, spec['sha'], None, extra)
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
    if cmd not in RUN_VERBS and cmd != GATE_VERB:
        return None
    framed = cmd in ('follow', 'fetch')
    try:
        p = read_params(stdin)
        if cmd in ('start', GATE_VERB):
            try:
                p = (check_spec if cmd == 'start' else check_gate_spec)(p)
            except SpecError:
                box.drop_refused(p, cmd == GATE_VERB)   # main pushed refs/node/<job> first
                raise
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
    d = getattr(box, cmd)(p if cmd in ('start', GATE_VERB) else p['job'])   # start gate clean stop
    _reply(write, d)
    return EXIT_OK if d.get('ok') else EXIT_REFUSED


def job_main(job, base, runner=None, verify=None):
    """`loadnode.py job <job>`: the detached runner. Points fds 1 and 2 at the job's log, turns
    SIGTERM into Ctrl+C (so the run is released and a result written), and runs it; `verify`
    reads a gate job's record (Box.run_gate)."""
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
    return box.run_job(job, verify=verify)
