"""Containment on Linux: the Job of kitlib.proc.job (docs/linux-support-plan.md section 2.3 and
section 8, slice 5). On Linux kitlib.proc.job.Job is this module's Job, with the same methods and
meanings, on the strongest tier the machine gives, probed at run time (probe()):

  S1  a cgroup v2 scope of the user's systemd (`systemd-run --user --scope -p Delegate=yes`) with
      the cpuset controller: the cpus pinned by the cgroup, the tree killed as one (cgroup.kill)
  S2  the same scope without cpuset (systemd's default delegation): kill and memory as S1, the
      cpus pinned by affinity only, which a child can set back
  W1  a PID namespace: probed and reported, not built (it needs unprivileged user namespaces)
  W2  no scope: kill by tree walk and an environment marker, the cpus by affinity, peak memory
      sampled; what it lacks is in its report

probe() never trusts an exit code: S1 and S2 come from a trial job, the real start path on
`sleep`, read back (its cgroup, cpuset.cpus.effective, its affinity, memory.peak) and killed
through cgroup.kill, and only a trial that is seen to die and whose scope is seen to go counts.
A process that itself runs in a kit job's leaf (a pool worker, a unit, a slot's command) reads the
tier from that job's cgroup instead, with no trial, and makes its jobs inside it (nesting, below).
Tier.text names the tier and what it lacks, the line the lock and its runs show (slice 7).

The layout of a job's cgroup J (S1, S2): the command and its tree in J/main, the kill_on_close
watchdog in J/w, and the jobs a member makes in J/j-<owner pid>-<n>-<hex>, so killing J kills the
jobs nested in it, as on Windows. A top-level J is the scope `<prefix>-job-<pid>-<n>-<hex>.scope`
(prefix: KIT_SCOPE_PREFIX, default pmkit); a nested J is a directory the owner makes in its own
job's cgroup (the scope is delegated, so the owner may), removed by close once it is empty.

Start, Windows' CREATE_SUSPENDED: the command runs through a sh stub that joins J/main (`echo 0`
to its cgroup.procs, so the stub moves itself), checks /proc/self/cgroup, forks the watchdog, says
so on a ready pipe and waits on a go pipe. Job.start reads the ready line, checks the stub's pid
and cgroup, pins, runs before_resume, then sends go, and the stub execs the command: no instant of
the command runs outside J or unpinned. systemd-run execs in place, so Popen's pid and returncode
are the command's. Variables added only so systemd-run reaches the user bus, and systemd's
INVOCATION_ID, are unset before the exec.

kill_on_close: the watchdog, a subshell of the stub in J/w that ignores INT, HUP, TERM and QUIT,
reads `arm`, `disarm` and `release` lines on a pipe whose write end only the owner holds. Release
ends it; end of file while armed (the owner closed the job, or died, SIGKILL included) writes
J/cgroup.kill, which ends it too. It is in J, so it never outlives J's kill, and it never kills
anything but J. Every write of cgroup.kill, the owner's and the watchdog's, targets J only: the
owner checks the path is a kit job's cgroup, named as this job named it, before it writes.

Stragglers (leftovers): end(kill=False) unpins J and releases the watchdog, so what the command
left keeps running unpinned in J, in the outer job when J is nested, as on Windows; leftovers()
moves them out of J into a scope of their own (top-level, through systemd) or a sibling job
directory (nested). Its call sites are slice 7's.

W2: the stub carries PMKIT_JOB, a list of job tokens each child inherits; the members are the
processes whose environment names the token plus the command's descendants. terminate stops every
member (SIGSTOP), then kills each (SIGKILL) through a pidfd opened before its identity is checked,
in bounded passes. Its watchdog is a separate python, in its own session, that kills by the token
on end of file while armed. Deviation from the plan: no PR_SET_CHILD_SUBREAPER. A subreaper must
be an ancestor, so the stub would have to be one, and the setting survives exec, so the command
would be made the subreaper of its own orphans and gather their zombies; the token finds orphans
instead. A process that empties its environment and leaves the tree is missed (the report says
so). processes() is None on every tier: a cgroup has no count of the processes ever started in it.

This module imports on any platform; its calls need Linux."""
import errno
import itertools
import os
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import threading
import time

from kitlib.platform.linux import cpu as lcpu
from kitlib.platform.linux import hardware
from kitlib.platform.linux import process as lproc

CGROOT = '/sys/fs/cgroup'
PROC = '/proc'
SH = '/bin/sh'
SYSTEMD_RUN = 'systemd-run'       # seams: a test points these at a fake
SYSTEMCTL = 'systemctl'
BUSCTL = 'busctl'
PREFIX_ENV = 'KIT_SCOPE_PREFIX'
PREFIX = 'pmkit'
MARK_ENV = 'PMKIT_JOB'
READY_SECONDS = 30.0              # a stub has this long to say it is in its cgroup
GONE_SECONDS = 5.0                # a killed job has this long to empty
TIDY_SECONDS = 0.5                # a released nested job has this long to empty before close
CALL_SECONDS = 30                 # a systemctl or busctl call
HELP_SECONDS = 5                  # systemd-run --help
KILL_PASSES = 8
CONTROLLERS = ('cpuset', 'cpu', 'memory', 'pids')
LEAF, WATCH_LEAF = 'main', 'w'
TOP = re.compile(r'[A-Za-z0-9_.-]+-job-\d+-\d+-[0-9a-f]+\.scope\Z')
NESTED = re.compile(r'j-\d+-\d+-[0-9a-f]+\Z')
_PREFIX_OK = re.compile(r'[A-Za-z0-9_.-]{1,40}\Z')
_count = itertools.count(1)

S2_LACK = 'cpu pinning by the cgroup (affinity only: a child can set its own back)'
PEAK_LACK = 'peak memory (sampled)'
W2_LACKS = ('a guaranteed kill (a tree walk and an environment marker: a process that empties '
            'its environment and leaves the tree is missed)',
            'cleanup when its watchdog is killed', 'cpu pinning (affinity only)', PEAK_LACK)
WHAT = {'S1': 'strong', 'S2': 'partial', 'W2': 'weak'}

# $1 job cgroup name, $2 the job cgroup to join ('' : the scope systemd put it in), $3 ready fd,
# $4 go fd, $5 watch fd, $6 armed (1/0), $7 variables to unset, then the command.
STUB = r'''n=$1 j=$2 r=$3 g=$4 k=$5 arm=$6 un=$7; shift 7
own() {
  cg=; while IFS= read -r l; do case $l in 0::*) cg=${l#0::};; esac; done </proc/self/cgroup
}
own
if [ -z "$j" ]; then
  case $cg in */"$n") j=$cg;; *) echo "x not in its scope: $cg" >&$r; exit 125;; esac
  mkdir "/sys/fs/cgroup$j/main" "/sys/fs/cgroup$j/w" ||
    { echo "x cannot make its leaves" >&$r; exit 125; }
fi
echo 0 >"/sys/fs/cgroup$j/main/cgroup.procs" || { echo "x cannot join $j/main" >&$r; exit 125; }
own
[ "$cg" = "$j/main" ] || { echo "x not in its leaf: $cg" >&$r; exit 125; }
( ( trap '' INT HUP TERM QUIT PIPE
    echo 0 >"/sys/fs/cgroup$j/w/cgroup.procs" || exit 125
    eval "exec $r>&- $g>&- 0<&$k $k<&-"
    exec >/dev/null 2>&1
    while IFS= read -r c; do
      case $c in arm) arm=1;; disarm) arm=0;; release) exit 0;; esac
    done
    [ "$arm" = 1 ] && echo 1 >"/sys/fs/cgroup$j/cgroup.kill"
  ) & echo "w $!" >&$r )
eval "exec $k<&-"
echo "s $$ $j" >&$r
IFS= read -r go <&$g
[ "$go" = go ] || exit 125
eval "exec $r>&- $g>&-"
for v in $un; do unset "$v"; done
exec "$@"
'''

# W2's stub: $1 ready fd, $2 go fd, then the command.
WSTUB = r'''r=$1 g=$2; shift 2
echo "s $$" >&$r
IFS= read -r go <&$g
[ "$go" = go ] || exit 125
eval "exec $r>&- $g>&-"
exec "$@"
'''

# W2's watchdog (python -I -S -c): argv token, watch fd, armed (1/0), passes.
WATCH = r'''import os, signal, sys, time
tok, fd, arm, passes = sys.argv[1], int(sys.argv[2]), sys.argv[3] == '1', int(sys.argv[4])
for s in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM, signal.SIGQUIT):
    signal.signal(s, signal.SIG_IGN)
spared = set()
with os.fdopen(fd, 'rb', 0) as f:
    for line in f:
        c = line.strip()
        if c == b'arm':
            arm = True
        elif c == b'disarm':
            arm = False
        elif c == b'release':
            sys.exit(0)
        elif c.startswith(b'spare '):
            spared.update(int(x) for x in c.split()[1:])
if not arm:
    sys.exit(0)
def marked(pid):
    try:
        with open('/proc/%d/environ' % pid, 'rb') as e:
            env = e.read().split(b'\0')
    except OSError:
        return False
    return any(v.startswith(b'PMKIT_JOB=') and tok.encode() in v[10:].split() for v in env)
me = os.getpid()
for _ in range(passes):
    fds = []
    for n in os.listdir('/proc'):
        if not n.isdigit() or int(n) == me or int(n) in spared or not marked(int(n)):
            continue
        try:
            pfd = os.pidfd_open(int(n))
        except OSError:
            continue
        if marked(int(n)):
            fds.append(pfd)
        else:
            os.close(pfd)
    if not fds:
        break
    for sig in (signal.SIGSTOP, signal.SIGKILL):
        for pfd in fds:
            try:
                signal.pidfd_send_signal(pfd, sig)
            except OSError:
                pass
    for pfd in fds:
        os.close(pfd)
    time.sleep(0.05)
'''

# The W1 trial (python -I -S -c): a user and PID namespace, and a child that is pid 1 in it.
W1_CODE = r'''import os, sys
try:
    os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWPID)
except (AttributeError, OSError) as e:
    print('user namespaces denied: %s' % e)
    sys.exit(0)
pid = os.fork()
if pid == 0:
    os._exit(0 if os.getpid() == 1 else 3)
_, st = os.waitpid(pid, 0)
print('ok' if os.waitstatus_to_exitcode(st) == 0 else 'the child was not pid 1')
'''


def _jobmod():
    from kitlib.proc import job
    return job


def _error(text):
    return _jobmod().JobError(text)


def prefix():
    """The first word of every top-level scope's name: KIT_SCOPE_PREFIX when it is a plain name
    (a test run's own, so its scopes can be listed and checked gone), else pmkit."""
    p = os.environ.get(PREFIX_ENV, '')
    return p if _PREFIX_OK.match(p) else PREFIX


def _tag():
    return '%d-%d-%s' % (os.getpid(), next(_count), secrets.token_hex(3))


def _read(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def _read_int(path):
    try:
        return int(_read(path).split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _write(path, text):
    with open(path, 'w', encoding='ascii') as f:
        f.write(text)


def cg_dir(cg):
    return CGROOT + cg


def cgroup_of(pid='self'):
    """The cgroup v2 path of pid ('/user.slice/...'), None when no such process; raises OSError
    when it cannot be read or has no cgroup v2 line."""
    try:
        text = _read('%s/%s/cgroup' % (PROC, pid))
    except (FileNotFoundError, ProcessLookupError):
        return None
    for line in text.splitlines():
        if line.startswith('0::'):
            return line[3:] or '/'
    raise OSError(errno.ENOENT, 'no cgroup v2 line in /proc/%s/cgroup' % pid)


def is_job_cgroup(cg):
    """True when cg is a cgroup a kit job made: a top-level kit job scope, or a nested job
    directory under one (each step a nested job name). Plain absolute paths only."""
    if not isinstance(cg, str) or not cg.startswith('/') or '\0' in cg:
        return False
    parts = cg.split('/')[1:]
    if any(p in ('', '.', '..') for p in parts):
        return False
    tops = [i for i, p in enumerate(parts) if TOP.match(p)]
    if not tops:
        return False
    return all(NESTED.match(p) for p in parts[tops[-1] + 1:])


def owned(cg, name):
    """True when cg is the job cgroup this job made: a kit job cgroup whose last step is `name`."""
    return is_job_cgroup(cg) and cg.rsplit('/', 1)[1] == name


def parent_job(own=None):
    """The job cgroup this process runs in (it is in that job's main leaf, with write access to
    the job's cgroup), or None."""
    try:
        cg = cgroup_of('self') if own is None else own
    except OSError:
        return None
    if not cg or not cg.endswith('/' + LEAF):
        return None
    parent = cg[:-len(LEAF) - 1]
    if not is_job_cgroup(parent) or not os.access(cg_dir(parent) + '/cgroup.procs', os.W_OK):
        return None
    return parent


def cgroup2():
    """True when /sys/fs/cgroup is a cgroup2 mount (mountinfo, not a guess)."""
    try:
        text = _read('%s/self/mountinfo' % PROC)
    except OSError:
        return False
    for line in text.splitlines():
        left, _, right = line.partition(' - ')
        f = left.split()
        if len(f) > 4 and f[4] == CGROOT and right.split()[:1] == ['cgroup2']:
            return True
    return False


def bus_env(env):
    """A copy of env that reaches the user's systemd bus (XDG_RUNTIME_DIR added when it is
    missing and /run/user/<uid>/bus exists), or None when no user bus can be named."""
    env = dict(env)
    if env.get('DBUS_SESSION_BUS_ADDRESS') or env.get('XDG_RUNTIME_DIR'):
        return env
    d = '/run/user/%d' % os.getuid()
    if os.path.exists(d + '/bus'):
        env['XDG_RUNTIME_DIR'] = d
        return env
    return None


def _tool(name):
    if os.sep in name:
        return name if os.access(name, os.X_OK) else None
    return shutil.which(name)


def _call(argv):
    """(True when it exited 0, its stderr) of a systemctl or busctl call; never trusted alone:
    each caller reads back what the call was to change."""
    env = bus_env(os.environ) or dict(os.environ)
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, timeout=CALL_SECONDS, creationflags=0)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, r.stderr.decode('utf-8', 'replace').strip()


_no_expand = {}


def no_expand(run):
    """['--expand-environment=no'] when systemd-run `run` names that switch in its --help, else
    []. systemd 254 added it (254 to 257: off by default with --scope; 258 turned it on), and an
    older one refuses it, but its --scope expands nothing: the stub's $ reach the shell either
    way. Asked once for each path."""
    if run not in _no_expand:
        try:
            out = subprocess.run([run, '--help'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=HELP_SECONDS,
                                 creationflags=0).stdout
        except (OSError, subprocess.SubprocessError):
            out = b''
        _no_expand[run] = ['--expand-environment=no'] if b'--expand-environment' in out else []
    return list(_no_expand[run])


def populated(cg):
    """True while cg or a cgroup under it holds a process; False when empty or gone."""
    try:
        for line in _read(cg_dir(cg) + '/cgroup.events').splitlines():
            if line.startswith('populated '):
                return line.split()[1] == '1'
    except OSError:
        return False
    return False


def wait_empty(cg, seconds):
    end = time.monotonic() + seconds
    pause = 0.001
    while populated(cg):
        if time.monotonic() >= end:
            return False
        time.sleep(pause)
        pause = min(pause * 2, 0.05)
    return True


def rmtree_cgroup(cg):
    """rmdir cg and the cgroups under it, deepest first; one still holding a process stays."""
    d = cg_dir(cg)
    for root, dirs, _files in os.walk(d, topdown=False):
        try:
            os.rmdir(root)
        except OSError:
            pass


def rss(pid):
    """Bytes resident of pid now (statm), 0 when gone."""
    try:
        return int(_read('%s/%d/statm' % (PROC, pid)).split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, IndexError):
        return 0


def vm_hwm(pid):
    """Bytes: the peak resident set of pid (VmHWM), 0 when gone."""
    try:
        for line in _read('%s/%d/status' % (PROC, pid)).splitlines():
            if line.startswith('VmHWM:'):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def cpu_ticks(pid):
    """utime + stime + cutime + cstime of pid in clock ticks, 0 when gone."""
    try:
        text = _read('%s/%d/stat' % (PROC, pid))
        f = text[text.rindex(')') + 2:].split()
        return sum(int(x) for x in f[11:15])
    except (OSError, ValueError, IndexError):
        return 0


def _pids():
    """Every pid PROC lists, or [] when it cannot be read."""
    try:
        return [int(n) for n in os.listdir(PROC) if n.isdigit()]
    except OSError:
        return []


def _stat_bytes(pid):
    with open('%s/%d/stat' % (PROC, pid), 'rb') as f:
        return f.read()


def pstat(pid, boot=None):
    """(ctime, ppid, exited) of pid from ONE read of its stat, so the parent and the identity are
    the same process's; None when it is gone or cannot be read."""
    try:
        data = _stat_bytes(pid)
        close = data.rindex(b')')
        f = data[close + 2:].split()
        return (lproc._ctime(int(f[19]), boot), int(f[1]),
                f[0].decode('ascii', 'replace') in lproc.EXITED)
    except (OSError, ValueError, IndexError):
        return None


def _boot():
    try:
        return lproc.boot()
    except OSError:
        return None


def tree(root, ctime, boot=None):
    """{pid: ctime} of root's descendants, or {} when root is not the process ctime names. Each
    child's parent and ctime come from one stat read, taken between two reads of the parent that
    find it unchanged, so a pid that ends and is reused while the walk runs never joins the tree
    (it is under its new parent, or its parent is gone)."""
    boot = _boot() if boot is None else boot
    if boot is None or ctime is None:
        return {}
    kids = {}
    for pid in _pids():
        st = pstat(pid, boot)
        if st is not None and pid != root:
            kids.setdefault(st[1], []).append(pid)
    out = {}
    queue = [(root, ctime)]
    while queue:
        q, qc = queue.pop(0)
        if not kids.get(q) or not _same(q, qc, boot):
            continue
        got = []
        for c in kids[q]:
            st = pstat(c, boot)
            if st is not None and st[1] == q and c not in out:
                got.append((c, st[0]))
        if not _same(q, qc, boot):
            continue
        for c, ct in got:
            out[c] = ct
            queue.append((c, ct))
    return out


def _same(pid, ctime, boot):
    st = pstat(pid, boot)
    return st is not None and st[0] == ctime


def marked(token):
    """{pid: ctime} of the processes whose environment's PMKIT_JOB names token (the ctime read
    before and after the environment, so the two are the same process's)."""
    want = token.encode('ascii')
    boot = _boot()
    out = {}
    if boot is None:
        return out
    for pid in _pids():
        st = pstat(pid, boot)
        if st is not None and _marks(pid, want) and _same(pid, st[0], boot):
            out[pid] = st[0]
    return out


def _marks(pid, want):
    try:
        with open('%s/%d/environ' % (PROC, pid), 'rb') as f:
            env = f.read().split(b'\0')
    except OSError:
        return False
    key = MARK_ENV.encode('ascii') + b'='
    return any(v.startswith(key) and want in v[len(key):].split() for v in env)


# ------------------------------------------------------------------ the tier
class Tier:
    """What probe() found: `name` S1, S2 or W2, `lacks` what it does not give, `why` the reason
    there is no scope (W2), `w1` the W1 trial's result (W2), `parent` the job cgroup a nested
    owner runs in. `text` is the line a status shows."""

    def __init__(self, name, lacks=(), why=None, w1=None, parent=None):
        self.name, self.lacks, self.why, self.w1 = name, tuple(lacks), why, w1
        self.parent = parent

    @property
    def scoped(self):
        return self.name in ('S1', 'S2')

    @property
    def text(self):
        parts = [self.name]
        if self.why:
            parts.append('no cgroup scope: %s' % self.why)
        if self.lacks:
            parts.append('lacks ' + ', '.join(self.lacks))
        if self.w1:
            parts.append(self.w1)
        return 'containment: %s (%s)' % (WHAT[self.name], '; '.join(parts))

    def __repr__(self):
        return '<Tier %s>' % self.text


_probe_lock = threading.Lock()
_probed = None


def probe(fresh=False):
    """This process's tier, found once (fresh: again)."""
    global _probed
    with _probe_lock:
        if _probed is None or fresh:
            _probed = _probe()
        return _probed


def _probe():
    parent = parent_job()
    if parent is not None:
        tier = nested_tier(parent)
        if tier is not None:
            return tier
        # a scope of its own would take its jobs out of the job it runs in
        return Tier('W2', W2_LACKS, why='it runs in kit job %s, which cannot hold a job' % parent,
                    w1=w1_status())
    name, lacks, why = trial()
    if name:
        return Tier(name, lacks)
    return Tier('W2', W2_LACKS, why=why, w1=w1_status())


def nested_tier(parent):
    """The tier of jobs made inside job cgroup `parent`, read from its controllers; None when it
    cannot hold one (no cgroup.kill, or its controllers cannot be read)."""
    d = cg_dir(parent)
    try:
        ctl = set(_read(d + '/cgroup.subtree_control').split())
    except OSError:
        return None
    if not os.path.exists(d + '/cgroup.kill'):
        return None
    lacks = [] if 'memory' in ctl else [PEAK_LACK]
    if 'cpuset' in ctl:
        return Tier('S1', lacks, parent=parent)
    return Tier('S2', [S2_LACK] + lacks, parent=parent)


def trial():
    """(tier name or None, lacks, why not) from a trial job: ScopeJob's own start on `sleep`,
    pinned to one cpu, read back and killed. The exit codes of systemd-run and of the trial are
    never what decides."""
    if not cgroup2():
        return None, (), 'no cgroup v2 mount at %s' % CGROOT
    if _tool(SYSTEMD_RUN) is None:
        return None, (), 'no systemd-run'
    if bus_env(os.environ) is None:
        return None, (), ('no user systemd bus (no XDG_RUNTIME_DIR, no /run/user/%d/bus)'
                          % os.getuid())
    try:
        want = max(os.sched_getaffinity(0))
    except (OSError, ValueError):
        return None, (), 'this process\'s affinity cannot be read'
    job = ScopeJob(1 << want, True, tier=Tier('S1'))
    p = None
    try:
        try:
            p = job.start(['sleep', '30'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
        except OSError as e:
            lack = ('' if no_expand(_tool(SYSTEMD_RUN)) else
                    '; its systemd-run --help names no --expand-environment')
            return None, (), 'a trial scope could not be made (%s)%s' % (e, lack)
        d = cg_dir(job.cg)
        try:
            eff = set(hardware.parse_cpulist(_read(d + '/cpuset.cpus.effective')))
        except (OSError, ValueError):
            eff = None
        try:
            aff = set(os.sched_getaffinity(p.pid))
        except OSError:
            aff = None
        pinned = eff == {want} and aff == {want}
        peak = _read_int(d + '/memory.peak')
        wrote = job.terminate()
        try:
            p.wait(GONE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        died = p.returncode == -signal.SIGKILL
        gone = _wait_gone(d, GONE_SECONDS)
        if not (wrote and died and gone):
            return None, (), ('the trial scope was not ended by its cgroup.kill (written %s, '
                              'the command %s, the scope %s)'
                              % (wrote, 'killed' if died else 'exit %r' % p.returncode,
                                 'gone' if gone else 'still there'))
    finally:
        job.close()
        if p is not None and p.poll() is None:
            p.kill()
            p.wait()
        if job.cg is not None and os.path.isdir(cg_dir(job.cg)):
            _call([_tool(SYSTEMCTL) or SYSTEMCTL, '--user', 'stop', job.unit + '.scope'])
    lacks = [] if peak is not None else [PEAK_LACK]
    if not pinned:
        return 'S2', [S2_LACK] + lacks, None
    return 'S1', lacks, None


def _wait_gone(d, seconds):
    end = time.monotonic() + seconds
    while os.path.isdir(d):
        if time.monotonic() >= end:
            return False
        time.sleep(0.01)
    return True


def w1_status():
    """'W1 available, not built', or 'W1 unavailable (<why>)', from a trial in a fresh python
    (read from what it prints, not its exit code)."""
    try:
        r = subprocess.run([sys.executable, '-I', '-S', '-c', W1_CODE], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
                           creationflags=0)
    except (OSError, subprocess.SubprocessError) as e:
        return 'W1 not known (its trial could not run: %s)' % e
    out = r.stdout.decode('utf-8', 'replace').strip()
    if out == 'ok':
        return 'W1 available, not built'
    return 'W1 unavailable (%s)' % (out or 'its trial said nothing')


# ------------------------------------------------------------------ the job
_mixed = {}


class Job:
    """kitlib.proc.job.Job on Linux: Job(mask=None, kill_on_close=True) is a ScopeJob (S1, S2) or
    a WeakJob (W2) as probe() finds (`tier` instead: a test's or the trial's). One command per
    job: start a second time and it raises JobError. `pin`, `effective` and `note` mean what
    they mean on Windows."""

    def __new__(cls, mask=None, kill_on_close=True, tier=None):
        if not issubclass(cls, (ScopeJob, WeakJob)):
            impl = ScopeJob if (tier or probe()).scoped else WeakJob
            if cls is not Job:              # a subclass of Job (a test's): mixed with the tier's
                key = (cls, impl)
                if key not in _mixed:
                    _mixed[key] = type(cls.__name__, (cls, impl), {})
                impl = _mixed[key]
            cls = impl
        return object.__new__(cls)

    def __init__(self, mask=None, kill_on_close=True, tier=None):
        self.tier = tier or probe()
        self.kill_on_close = bool(kill_on_close)
        self.note, self.p, self.closed = None, None, False
        self._child, self._k, self._g, self._peak = None, None, None, 0
        every = hardware.all_mask()
        self.mask = (mask & every) if mask is not None else None
        if mask is not None and not self.mask:
            self.note, self.mask = ('this machine has none of cores %s'
                                    % _jobmod().cores_text(mask)), None
        self.wd = None
        self.pin = 'pinned' if self.mask is not None else None
        self.effective = self.mask

    # ---- start
    def start(self, cmd, before_resume=None, **kw):
        """Popen(cmd) through the stub, in the job and pinned before it runs, then let go; as
        kitlib.proc.job.Job.start on Windows (Ctrl+C held the same way, _Held). Raises OSError
        when the command cannot start (FileNotFoundError when it is not found), and JobError when
        it cannot be put in the job; the stub is then ended. `creationflags` are Windows's and
        dropped; shell=True runs cmd under /bin/sh."""
        if self._child is not None or self.closed:
            raise _error('a job on Linux starts one command; this one has started one or is closed')
        kw.pop('creationflags', None)
        if kw.pop('shell', False):
            args = [cmd] if isinstance(cmd, (str, bytes)) else list(cmd)
            cmd = [SH, '-c'] + args
        elif isinstance(cmd, (str, bytes)):
            cmd = [cmd]
        cmd = [os.fsdecode(a) for a in cmd]
        if not cmd:
            raise ValueError('an empty command')
        if kw.get('executable') is not None:
            raise ValueError('Job.start on Linux takes no executable=')
        env = dict(os.environ if kw.get('env') is None else kw['env'])
        _find(cmd[0], env, kw.get('cwd'))
        held = _jobmod()._Held()
        try:
            p = self._begin(cmd, env, kw)
            try:
                self._pin_start(p)
                self.check_pin(p)
                if before_resume is not None:
                    before_resume(p)
                held.deliver()
                self._go()
                held.deliver()
            except BaseException:
                self._abort(p)
                raise
            self.p = p
        finally:
            held.close()
        return p

    def _ready(self, p, fd):
        """{kind: words} of the stub's ready lines up to its `s` line; JobError when it ends or
        says `x` first, or is silent READY_SECONDS."""
        buf, lines = b'', {}
        end = time.monotonic() + READY_SECONDS
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        while 's' not in lines:
            left = end - time.monotonic()
            if left <= 0:
                raise _error('the command was not in its job %ds after its start' % READY_SECONDS)
            if not poller.poll(min(left, 0.25) * 1000):
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                why = ' '.join(lines['x']) if 'x' in lines else None
                if why is None:
                    try:
                        why = 'its starter ended first (exit %s)' % p.wait(GONE_SECONDS)
                    except subprocess.TimeoutExpired:
                        why = 'its starter closed the ready pipe'
                raise _error('the command could not be put in its job: %s' % why)
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                words = line.decode('utf-8', 'replace').split()
                if words:
                    lines[words[0]] = words[1:]
        return lines

    def _go(self):
        try:
            os.write(self._g, b'go\n')
        except OSError as e:
            raise _error('the command ended before it was let go (%s)' % e)
        finally:
            _close(self._g)
            self._g = None

    def _abort(self, p):
        """A start that failed: the job's processes ended (the stub at least), its pipes closed,
        so its watchdog ends too."""
        try:
            if not self._kill_all():
                p.kill()
        except OSError:
            p.kill()
        p.wait()
        _close(self._g)
        _close(self._k)
        self._g = self._k = None
        self._reap_watchdog()

    def _reap_watchdog(self):
        pass

    def assign(self, proc):
        """Puts a live process (a Popen, or a pid) in the job; raises JobError when it cannot."""
        raise _error('a job on Linux holds only the command it started (W2 has no way to add one)')

    def assign_self(self):
        self.assign(os.getpid())

    # ---- pin
    def check_pin(self, proc):
        """Reads proc's affinity back: a limit outside the job (an outer cpuset) wins, so a mask
        it overrides makes pin "overridden" and effective what it runs on."""
        if self.pin != 'pinned' or self.mask is None:
            return self.pin
        try:
            got = lcpu.cpus_mask(os.sched_getaffinity(int(getattr(proc, 'pid', proc))))
        except OSError:
            return self.pin
        if got != self.mask:
            mt = _jobmod().cpu.mask_text
            self.pin, self.effective = 'overridden', got
            self.note = ('a limit outside the job wins: the command runs on cpus %s, not %s'
                         % (mt(got), mt(self.mask)))
        return self.pin

    def _pin_affinity(self, pids, mask):
        ids = lcpu.mask_cpus(mask) if mask is not None else hardware.threads()
        ok = True
        for pid in pids:
            if lcpu.set_threads(pid, ids) not in ('pinned', 'gone'):
                ok = False
        return ok

    def narrow(self, mask):
        """Pins the pinned job to `mask`, a non-empty part of its mask, while it runs; False, the
        pin unchanged, when it is not pinned, `mask` is not part of its mask, or it is refused."""
        if self.closed or self.pin != 'pinned' or self.mask is None:
            return False
        if not mask or mask & ~self.mask:
            return False
        return self.repin(mask)

    def repin(self, mask):
        """Sets the pinned job's mask to `mask` (narrow's undo); False, unchanged, when refused."""
        if self.closed or self.pin != 'pinned' or not mask:
            return False
        if self._child is None:
            self.mask = self.effective = mask
            return True
        if not self._set_pin(mask):
            self._set_pin(self.mask)
            return False
        self.mask = self.effective = mask
        return True

    def unpin(self):
        """The processes left in the job get every cpu back, and the job drops kill_on_close."""
        if self.mask is not None and self._child is not None:
            self._set_pin(None)
        self.mask = self.pin = self.effective = None
        self.kill_on_close = False
        self._watch('disarm')

    # ---- read
    def processes(self):
        """None on Linux: no tier counts the processes ever started in a job."""
        return None

    def tree_working_set(self):
        """Bytes: the resident sets of the job's processes now, summed."""
        try:
            return sum(rss(pid) for pid in self.pids())
        except OSError:
            return 0

    # ---- end
    def set_kill_on_close(self, on):
        self.kill_on_close = bool(on)
        return self._watch('arm' if on else 'disarm')

    def _watch(self, line):
        if self._k is None:
            return False
        try:
            os.write(self._k, (line + '\n').encode('ascii'))
            return True
        except OSError:
            return False

    def end(self, kill):
        """kill: end every process in the job. Else let them live, unpinned. Closes the job."""
        if self.closed:
            if kill and self.p is not None and self.p.poll() is None:
                self.p.kill()
            return
        if kill:
            self.terminate(1)
        else:
            self.unpin()
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _close(fd):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _find(prog, env, cwd):
    """Raise as Popen would when prog cannot be run: the stub's exec would fail too late."""
    if '/' in prog:
        path = prog if os.path.isabs(prog) or not cwd else os.path.join(os.fspath(cwd), prog)
        if not os.path.isfile(path):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), prog)
        if not os.access(path, os.X_OK):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), prog)
        return
    if shutil.which(prog, path=env.get('PATH', os.defpath)) is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), prog)


# dash, the usual /bin/sh, takes a single-digit descriptor only in a redirection (`>&12` is a
# syntax error), and a busy process's pipes are past 9: this moves the stub's pipes to 3, 4, 5...
# (FDS), closes the originals and execs the stub, the pid unchanged. The copies above 9 are
# close-on-exec, so the stub holds its pipes once each.
REMAP = ('import fcntl,os,sys\n'
         'a=sys.argv;n=int(a[1]);fds=[int(x) for x in a[2:2+n]]\n'
         'hi=[fcntl.fcntl(f,fcntl.F_DUPFD_CLOEXEC,10) for f in fds]\n'
         'for f in fds:os.close(f)\n'
         'for i,f in enumerate(hi):os.dup2(f,3+i)\n'
         'os.execv(a[2+n],a[2+n:])\n')
FDS = ('3', '4', '5')


def _popen(argv, kw, env, fds):
    """subprocess.Popen of the stub (argv, from its shell on) with the caller's keywords, its
    pipes `fds` moved to FDS first (REMAP)."""
    if kw.get('pass_fds'):
        raise ValueError('Job.start on Linux takes no pass_fds=: the stub owns descriptors 3-5')
    py = sys.executable
    if not py or not os.path.isabs(py):
        raise _error('no interpreter path to start the stub with (sys.executable is %r)' % py)
    kw = dict(kw)
    kw['env'] = env
    kw['pass_fds'] = tuple(fds)
    kw['close_fds'] = True
    lead = [py, '-I', '-S', '-c', REMAP, str(len(fds))] + [str(f) for f in fds]
    return subprocess.Popen(lead + list(argv), creationflags=0, **kw)


class ScopeJob(Job):
    """S1 and S2: the job is a cgroup, J (the module docstring has its layout)."""

    def __init__(self, mask=None, kill_on_close=True, tier=None):
        Job.__init__(self, mask, kill_on_close, tier)
        self.cg = self.unit = self.name = None
        self.parent = self.tier.parent
        self.cpuset = self.tier.name == 'S1'     # the pin is the cgroup's, not affinity

    def _begin(self, cmd, env, kw):
        r_r, r_w = os.pipe()
        g_r, g_w = os.pipe()
        k_r, k_w = os.pipe()
        self._g, self._k = g_w, k_w
        made, p = None, None
        try:
            unset = []
            if self.parent is None:
                self.unit = '%s-job-%s' % (prefix(), _tag())
                self.name = self.unit + '.scope'
                run = _tool(SYSTEMD_RUN)
                benv = bus_env(env)
                if run is None or benv is None:
                    raise _error('no systemd-run or no user bus for a scope')
                unset = [n for n in benv if n not in env]
                if 'INVOCATION_ID' not in env:
                    unset.append('INVOCATION_ID')
                props = ['-p', 'Delegate=yes']
                if self.mask is not None and self.cpuset:
                    props += ['-p', 'AllowedCPUs=' + _jobmod().cpu.mask_text(self.mask)]
                argv = ([run, '--user', '--scope', '--quiet', '--collect'] + no_expand(run)
                        + ['--unit', self.unit] + props + ['--'])
                join, penv = '', benv
            else:
                self.name = 'j-' + _tag()
                made = self.parent + '/' + self.name
                self._make_nested(made)
                argv, join, penv = [], made, env
            argv += [SH, '-c', STUB, 'pmkit-job', self.name, join] + list(FDS) + [
                '1' if self.kill_on_close else '0', ' '.join(unset)] + cmd
            p = _popen(argv, kw, penv, (r_w, g_r, k_r))
        except BaseException:
            for fd in (r_r, r_w, g_r, g_w, k_r, k_w):
                _close(fd)
            self._g = self._k = None
            if made is not None:
                rmtree_cgroup(made)
            raise
        for fd in (r_w, g_r, k_r):
            _close(fd)
        try:
            try:
                lines = self._ready(p, r_r)
            finally:
                _close(r_r)
            if lines.get('w'):
                self.wd = int(lines['w'][0])
            s = lines['s']
            if len(s) != 2 or int(s[0]) != p.pid:
                raise _error('the ready line named pid %s, not the command\'s %d' % (s[:1], p.pid))
            cg = s[1]
            if made is not None and cg != made or not owned(cg, self.name):
                raise _error('the command is in %s, not a cgroup this job made' % cg)
            if cgroup_of(p.pid) != cg + '/' + LEAF:
                raise _error('the command is not in its job\'s leaf %s/%s' % (cg, LEAF))
            self.cg = cg
            if made is None:
                self._enable(cg)
        except BaseException:
            self._abort(p)
            if made is not None and wait_empty(made, GONE_SECONDS):
                rmtree_cgroup(made)
            raise
        self._child = p
        return p

    def assign(self, proc):
        """Puts a live process (a Popen, or a pid) in the job's main leaf; raises JobError when
        the job has no cgroup yet or the move is refused."""
        pid = int(getattr(proc, 'pid', proc))
        if self.cg is None or self.closed:
            raise _error('the job has no cgroup yet: it gets one when it starts its command')
        try:
            _write(cg_dir(self.cg) + '/' + LEAF + '/cgroup.procs', str(pid))
        except OSError as e:
            raise _error('pid %d could not be put in %s (%s)' % (pid, self.cg, e))

    def _make_nested(self, cg):
        d = cg_dir(cg)
        os.mkdir(d)
        try:
            self._enable(cg)
            os.mkdir(d + '/' + LEAF)
            os.mkdir(d + '/' + WATCH_LEAF)
            if self.mask is not None and self.cpuset:
                if os.path.exists(d + '/cpuset.cpus'):
                    _write(d + '/cpuset.cpus', _jobmod().cpu.mask_text(self.mask))
                else:
                    self.cpuset = False
                    self.note = 'the cpus are pinned by affinity only: the outer job has no cpuset'
        except OSError as e:
            rmtree_cgroup(cg)
            raise _error('the job cgroup %s could not be made (%s)' % (cg, e))

    def _enable(self, cg):
        """Turns on, for the cgroups under cg, the controllers cg has (nested jobs pin and
        measure with them). Best effort: a controller left off shows in nested_tier."""
        d = cg_dir(cg)
        try:
            have = set(_read(d + '/cgroup.controllers').split())
        except OSError:
            return
        want = ' '.join('+' + c for c in CONTROLLERS if c in have)
        if want:
            try:
                _write(d + '/cgroup.subtree_control', want)
            except OSError:
                pass

    def _pin_start(self, p):
        if self.mask is None or self.cpuset:
            return
        if not self._pin_affinity([p.pid], self.mask):
            self.note = 'the core pin could not be set (affinity refused)'
            self.mask = self.pin = self.effective = None

    def _set_pin(self, mask):
        """Pins J to mask (None: every cpu) and reads it back; True when it holds."""
        if self.cg is None:
            return False
        if not self.cpuset:
            return self._pin_affinity(self.pids(), mask)
        text = _jobmod().cpu.mask_text(mask) if mask is not None else ''
        d = cg_dir(self.cg)
        if self.parent is None:
            _call([_tool(SYSTEMCTL) or SYSTEMCTL, '--user', 'set-property', '--runtime',
                   self.name, 'AllowedCPUs=' + text])
        else:
            try:
                _write(d + '/cpuset.cpus', text or '\n')
            except OSError:
                return False
        want = set(lcpu.mask_cpus(mask)) if mask is not None else None
        end = time.monotonic() + 2.0
        while True:
            try:
                eff = set(hardware.parse_cpulist(_read(d + '/cpuset.cpus.effective')))
            except (OSError, ValueError):
                eff = None
            if eff is not None and (eff == want if want is not None else
                                    eff == set(hardware.threads()) or not text):
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.01)

    # ---- read
    def pids(self):
        """The pids in J now (its main leaf and the jobs nested in it, never a watchdog);
        [] once J is gone; raises OSError when J cannot be read."""
        if self.cg is None:
            return []
        top = cg_dir(self.cg)
        out = []
        for root, dirs, _files in os.walk(top):
            dirs[:] = [x for x in dirs if x != WATCH_LEAF]
            try:
                text = _read(root + '/cgroup.procs')
            except FileNotFoundError:
                continue
            out.extend(int(x) for x in text.split())
        return [p for p in out if p != self.wd]

    def contains(self, pid):
        """"in" (J or a job nested in it holds it), "out", "gone" or "unknown"."""
        pid = int(pid)
        if self.cg is None or pid <= 0:
            return 'unknown'
        try:
            cg = cgroup_of(pid)
        except OSError:
            return 'unknown'
        if cg is None:
            return 'gone'
        return 'in' if cg == self.cg or cg.startswith(self.cg + '/') else 'out'

    def usage(self):
        """(CPU seconds so far, J's peak memory (memory.peak; else the most seen), its tree's
        resident set now), or None (not started, closed or gone)."""
        if self.cg is None or self.closed:
            return None
        d = cg_dir(self.cg)
        try:
            usec = None
            for line in _read(d + '/cpu.stat').splitlines():
                if line.startswith('usage_usec '):
                    usec = int(line.split()[1])
            if usec is None:
                return None
        except (OSError, ValueError):
            return None
        ws = self.tree_working_set()
        peak = _read_int(d + '/memory.peak')
        if peak is None:
            self._peak = max(self._peak, _read_int(d + '/memory.current') or ws)
            peak = self._peak
        own = vm_hwm(self.p.pid) if self.p is not None else 0
        return (usec / 1e6, peak, max(own, ws))

    # ---- end
    def _kill_all(self):
        """Writes J's cgroup.kill; True written or J gone, False when no J is known or the write
        is refused. Raises JobError, writing nothing, when J is not a cgroup this job made."""
        if self.cg is None:
            return False
        if not owned(self.cg, self.name) or (self.parent is not None and
                                             self.cg != self.parent + '/' + self.name):
            raise _error('refused to kill %r: not the cgroup this job made' % self.cg)
        try:
            _write(cg_dir(self.cg) + '/cgroup.kill', '1')
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def terminate(self, code=1):
        """Kills every process in J (cgroup.kill; a signal carries no exit code). Only starts each
        end, as on Windows."""
        return self._kill_all()

    def leftovers(self, pids=None):
        """Moves pids (default: every process in J) out of J, into a scope of their own
        (top-level J) or a sibling job directory (nested J), unpinned and out of J's kill.
        Returns the pids moved; raises JobError when one could not be moved."""
        if self.cg is None:
            return []
        pids = [p for p in (self.pids() if pids is None else pids)
                if p != self.wd and self.contains(p) == 'in']
        if not pids:
            return []
        if self.parent is None:
            unit = '%s-leftovers-%s.scope' % (prefix(), _tag())
            _call([_tool(BUSCTL) or BUSCTL, '--user', 'call', 'org.freedesktop.systemd1',
                   '/org/freedesktop/systemd1', 'org.freedesktop.systemd1.Manager',
                   'StartTransientUnit', 'ssa(sv)a(sa(sv))', unit, 'fail', '2', 'PIDs', 'au',
                   str(len(pids))] + [str(p) for p in pids] + ['CollectMode', 's',
                                                               'inactive-or-failed', '0'])
            dest = None
        else:
            dest = self.parent + '/j-' + _tag()
            try:
                os.mkdir(cg_dir(dest))
                for p in pids:
                    try:
                        _write(cg_dir(dest) + '/cgroup.procs', str(p))
                    except ProcessLookupError:
                        pass
            except OSError as e:
                raise _error('the leftovers could not be moved to %s (%s)' % (dest, e))
        end = time.monotonic() + GONE_SECONDS
        while True:
            still = [p for p in pids if self.contains(p) == 'in']
            if not still:
                return pids
            if time.monotonic() >= end:
                raise _error('leftovers %s are still in the job' % still)
            time.sleep(0.01)

    def close(self):
        """Kill-on-close: kills J. Else releases the watchdog, so what runs in J runs on. A
        nested J is removed once it is empty (TIDY_SECONDS at most for a released one)."""
        if self.closed:
            return
        self.closed = True
        killed = False
        if self.kill_on_close and self.cg is not None:
            try:
                killed = self._kill_all()
            except OSError:
                killed = False
        elif self._k is not None:
            self._watch('release')
        _close(self._k)
        self._k = None
        _close(self._g)
        self._g = None
        if self.parent is not None and self.cg is not None:
            if wait_empty(self.cg, GONE_SECONDS if killed else TIDY_SECONDS):
                rmtree_cgroup(self.cg)


class WeakJob(Job):
    """W2: no cgroup. The members are the processes whose PMKIT_JOB names this job's token, and
    the command's descendants."""

    def __init__(self, mask=None, kill_on_close=True, tier=None):
        Job.__init__(self, mask, kill_on_close, tier)
        self.token = 'w' + _tag()
        self.spared = set()
        self._ctime = None

    def _begin(self, cmd, env, kw):
        env = dict(env)
        env[MARK_ENV] = (env.get(MARK_ENV, '') + ' ' + self.token).strip()
        r_r, r_w = os.pipe()
        g_r, g_w = os.pipe()
        self._g = g_w
        try:
            p = _popen([SH, '-c', WSTUB, 'pmkit-job'] + list(FDS[:2]) + cmd, kw, env,
                       (r_w, g_r))
        except BaseException:
            for fd in (r_r, r_w, g_r, g_w):
                _close(fd)
            self._g = None
            raise
        _close(r_w)
        _close(g_r)
        try:
            try:
                lines = self._ready(p, r_r)
            finally:
                _close(r_r)
            if lines['s'][:1] != [str(p.pid)]:
                raise _error('the ready line named pid %s, not the command\'s %d'
                             % (lines['s'][:1], p.pid))
            self._ctime = lproc.proc_ctime(p.pid)
            self._child = p
            self._spawn_watchdog()
        except BaseException:
            self._abort(p)
            raise
        return p

    def _spawn_watchdog(self):
        k_r, k_w = os.pipe()
        try:
            self.wd = subprocess.Popen(
                [sys.executable, '-I', '-S', '-c', WATCH, self.token, str(k_r),
                 '1' if self.kill_on_close else '0', str(KILL_PASSES)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(k_r,), close_fds=True, start_new_session=True, creationflags=0)
        except OSError as e:
            _close(k_w)
            raise _error('the job\'s watchdog could not start (%s)' % e)
        finally:
            _close(k_r)
        self._k = k_w

    def _pin_start(self, p):
        if self.mask is not None and not self._pin_affinity([p.pid], self.mask):
            self.note = 'the core pin could not be set (affinity refused)'
            self.mask = self.pin = self.effective = None

    def _set_pin(self, mask):
        return self._pin_affinity(self.pids(), mask)

    def _proc(self):
        return self._child

    def _reap_watchdog(self):
        if self.wd is not None:
            try:
                self.wd.wait(GONE_SECONDS)
            except subprocess.TimeoutExpired:
                pass

    def members(self):
        """{pid: ctime} of the job's processes now."""
        boot = _boot()
        found = marked(self.token)
        p = self._proc()
        if p is not None and self._ctime is not None and boot is not None:
            found[p.pid] = self._ctime
            for pid, ct in tree(p.pid, self._ctime, boot).items():
                found.setdefault(pid, ct)
        for pid, ct in list(found.items()):
            st = pstat(pid, boot) if boot is not None else None
            if (pid in self.spared or (self.wd is not None and pid == self.wd.pid)
                    or st is None or st[0] != ct or st[2]):
                del found[pid]
        return found

    def pids(self):
        return sorted(self.members())

    def contains(self, pid):
        pid = int(pid)
        if pid <= 0:
            return 'unknown'
        if pid in self.members():
            return 'in'
        return 'gone' if lproc.proc_ctime(pid) is None else 'out'

    def usage(self):
        """(CPU seconds of the live members and their reaped children, the most memory seen
        (sampled at each call), the tree's resident set now), or None (closed or not started)."""
        if self.closed or self._proc() is None:
            return None
        pids = self.pids()
        ticks = sum(cpu_ticks(pid) for pid in pids)
        ws = sum(rss(pid) for pid in pids)
        self._peak = max(self._peak, ws)
        own = vm_hwm(self._proc().pid)
        return (ticks / float(os.sysconf('SC_CLK_TCK')), self._peak, max(own, ws))

    def _kill_pass(self):
        """One pass: every member stopped, then killed, each through a pidfd opened before its
        identity is checked. Returns how many it reached."""
        fds = []
        boot = _boot()
        for pid, ct in self.members().items():
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                continue
            # the pidfd pins one process: the one with ct, or a reuse of the pid, which is let go
            st = pstat(pid, boot)
            if st is None or st[0] != ct:
                _close(fd)
                continue
            fds.append(fd)
        for sig in (signal.SIGSTOP, signal.SIGKILL):
            for fd in fds:
                try:
                    signal.pidfd_send_signal(fd, sig)
                except OSError:
                    pass
        for fd in fds:
            _close(fd)
        return len(fds)

    def _kill_all(self):
        if self._proc() is None:
            return False
        for _ in range(KILL_PASSES):
            if not self._kill_pass():
                return True
            time.sleep(0.02)
        return not self.members()

    def terminate(self, code=1):
        """Kills every member it can find, in bounded passes; True when none is left."""
        return self._kill_all()

    def leftovers(self, pids=None):
        """Takes pids (default: every member) out of the job's kill, and its watchdog's."""
        pids = [p for p in (self.pids() if pids is None else pids) if p in self.members()]
        self.spared.update(pids)
        if pids:
            self._watch('spare ' + ' '.join(str(p) for p in pids))
        return pids

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.kill_on_close and self._proc() is not None:
            self._kill_all()
        elif self._k is not None:
            self._watch('release')
        _close(self._k)
        self._k = None
        _close(self._g)
        self._g = None
        self._reap_watchdog()
