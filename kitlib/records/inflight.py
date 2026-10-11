"""Is a landing in flight, and waiting for it to end: what new_worktree and sync_base read before
they fetch (docs/new-worktree.md "Waiting for a landing in flight"; docs/sync-base.md).

`.kit.json` "landing".in_flight says how to tell; unset, nothing here runs (`reader()` is None):

- `{}` (no command): the kit landing lock (kitlib.records.landing, the file `gate.py land` holds
  from its merge to its push or undo). Held, or a file that is no lock record: in flight.
- `{"command": [argv]}`: a repository whose landing tool keeps its own lock. The argv runs with
  the main checkout as its working folder, no input and no shell (`{python}` is this Python,
  `{root}` the main checkout), so `["{python}", "tools/x.py"]` runs the repository's tools/x.py.
  The program itself is found by the Windows CreateProcess search, not from the root: a bare
  name gets only `.exe` added, a .cmd, .bat or .ps1 needs its interpreter first in the argv, and a
  relative program path resolves against the caller's folder.
  One look gets COMMAND_TIMEOUT seconds, or what is left of wait_s when that is less (never under
  LOOK_MIN_S). Exit 0: in flight, and the first line it prints names the holder. Exit 1: none.
  Anything else, a timeout, or a command that cannot start: in flight, because "cannot tell"
  must not read as "free".
- `wait_s`: the most seconds from the first look to going on (unset: WAIT_S). A look still
  running at the bound ends by its own capped timeout, plus KILL_GRACE at worst.

A reader only reads; it never takes, breaks or changes a lock. Nothing here prints: `wait()` hands
its progress lines to the caller's `say`.
"""
import subprocess
import sys
import time

from kitlib.git import cmd as gitcmd
from kitlib.proc import job as _job
from kitlib.records import landing

# Inferred, not measured: under the agent shell's default 120 s command timeout, so a wait that
# runs out still leaves the tool time to fetch and finish.
WAIT_S = 100
POLL_S = 10               # seconds between looks while waiting
NOTE_EVERY_S = 60         # seconds between "still waiting" lines
COMMAND_TIMEOUT = 30      # seconds one look by command may take
LOOK_MIN_S = 2            # the least a look by command gets, however little of wait_s is left
KILL_GRACE = 5            # seconds a timed-out command's pipes get to close after its tree is ended
HOLDER_MAX = 200          # characters of the holder line kept
HELD, FREE = 0, 1         # the command's exit codes


class LockReader(object):
    """In flight while the kit landing lock is held (a file that is no lock record is held)."""

    kind = 'lock'

    def __init__(self, lock, wait_s=WAIT_S):
        self.lock, self.wait_s = lock, wait_s

    def look(self, left=None):
        """`(held, holder)`; holder says who, or why a held lock names nobody. Reads only. A file
        read is quick, so `left` (the seconds of the wait left) is not needed."""
        try:
            record = self.lock.read()
        except landing.LockError as e:
            return True, 'the landing lock %s cannot be read (%s), treated as held' % (
                self.lock.path, e)
        if record is None:
            return False, 'nobody holds the landing lock (%s)' % self.lock.path
        return True, 'the landing lock %s is held: %s' % (self.lock.path, landing.describe(record))


class CommandReader(object):
    """In flight when the configured command says so (exit 0), or cannot say (not exit 1)."""

    kind = 'command'

    def __init__(self, argv, root, wait_s=WAIT_S, timeout=COMMAND_TIMEOUT):
        self.argv = [a.replace('{python}', sys.executable).replace('{root}', root) for a in argv]
        self.root, self.wait_s, self.timeout = root, wait_s, timeout

    def shown(self):
        return ' '.join(self.argv)

    def look(self, left=None):
        """`(held, holder)`. Runs the command once, for its timeout or `left` (the seconds of the
        wait left; None: no wait to keep to), whichever is less, but never under LOOK_MIN_S."""
        timeout = self.timeout if left is None else max(LOOK_MIN_S, min(self.timeout, left))
        try:
            rc, out, err = run(self.argv, self.root, timeout)
        except subprocess.TimeoutExpired:
            return True, 'cannot tell: `%s` did not answer within %s seconds, treated as in ' \
                         'flight' % (self.shown(), _secs(timeout))
        except OSError as e:
            return True, 'cannot tell: `%s` could not run (%s), treated as in flight' % (
                self.shown(), e)
        said = first_line(out)
        if rc == HELD:
            return True, said or '`%s` says a landing is in flight (it named no holder)' % \
                self.shown()
        if rc == FREE:
            return False, said or '`%s` says no landing is in flight' % self.shown()
        why = first_line(err) or said
        return True, 'cannot tell: `%s` exited %s%s, treated as in flight' % (
            self.shown(), rc, (' (%s)' % why) if why else '')


def _secs(n):
    """n seconds as a whole number when it is one, else to a tenth."""
    return '%d' % n if n == int(n) else '%.1f' % n


def time_left(reader_, start, clock=time.monotonic):
    """Seconds of reader_.wait_s left since `start` (a `clock()` reading), never below 0."""
    return max(0, reader_.wait_s - (clock() - start))


def first_line(data):
    """The first non-blank line of command output (bytes), cut to HOLDER_MAX characters."""
    for line in gitcmd.text(data or b'').splitlines():
        if line.strip():
            line = line.strip()
            return line if len(line) <= HOLDER_MAX else line[:HOLDER_MAX - 3] + '...'
    return ''


def run(argv, cwd, timeout):
    """`(rc, stdout, stderr)` bytes of argv run in `cwd` in its own job, with no input and the
    environment kitlib.git.cmd.scrubbed_env() gives (an inherited GIT_DIR would make a git in it
    answer about another repository). Raises OSError when it cannot start, and
    subprocess.TimeoutExpired after `timeout` seconds, the job's whole tree ended first."""
    job = _job.Job()                       # kill-on-close: nothing it started outlives the look
    try:
        p = job.start(argv, cwd=cwd, env=gitcmd.scrubbed_env(), stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            job.terminate()
            try:
                p.communicate(timeout=KILL_GRACE)
            except subprocess.TimeoutExpired:
                pass
            raise
        return p.returncode, out, err
    finally:
        job.close()


def reader(root, cfg, lock):
    """The reader `.kit.json` "landing".in_flight names, or None when it is unset (off). `root` is
    the main checkout, `lock` the kit landing lock (kitlib.records.landing.LandingLock)."""
    spec = cfg.section('landing').get('in_flight')
    if spec is None:
        return None
    wait_s = spec.get('wait_s')
    wait_s = WAIT_S if wait_s is None else wait_s
    if spec.get('command'):
        return CommandReader(spec['command'], root, wait_s)
    return LockReader(lock, wait_s)


def settle(reader_, wait_=True, say=None, sleep=time.sleep, clock=time.monotonic, start=None):
    """One look, and the wait when a landing is in flight: `(held, lines)`. held: still in flight
    when the caller goes on (the bound came first, or `wait_` False). lines are the caller's
    output: none when no landing was in flight; else one saying how it ended. The lines said
    while waiting go to `say` as they happen (None: they lead `lines` instead). wait_s counts
    from `start` (a `clock()` reading; None: now, before the first look), so the time the looks
    take is part of the wait."""
    live = []
    say = say or live.append
    start = clock() if start is None else start
    held, holder = reader_.look(time_left(reader_, start, clock))
    if not held:
        return False, []
    if not wait_:
        return True, ['landing   in flight, not waited for (--no-wait): %s' % holder]
    say('landing   in flight: %s; waiting up to %ds (--no-wait goes on now)'
        % (holder, reader_.wait_s))
    held, holder, waited = wait(reader_, holder, say, sleep, clock, start=start)
    if held:
        return True, live + ['landing   still in flight after %ds, going on without it: %s'
                             % (waited, holder)]
    return False, live + ['landing   done after %ds' % waited]


def wait(reader_, holder, say, sleep=time.sleep, clock=time.monotonic, poll_s=POLL_S,
         every_s=NOTE_EVERY_S, start=None):
    """Waits up to reader_.wait_s seconds from `start` (a `clock()` reading; None: now) for a
    landing seen in flight (`holder`) to end, looking every `poll_s` seconds, each look given
    only what is left of the wait. `(held, holder, waited_s)`: held False once a look says it
    ended, True when the bound came first (holder then is the last look's). `say(line)` gets a
    line each `every_s` seconds of waiting."""
    start = clock() if start is None else start
    noted = every_s
    while True:
        waited = clock() - start
        if waited >= reader_.wait_s:
            return True, holder, waited
        sleep(min(poll_s, reader_.wait_s - waited))
        held, now = reader_.look(time_left(reader_, start, clock))
        waited = clock() - start
        if not held:
            return False, holder, waited
        holder = now
        if waited >= noted and waited < reader_.wait_s:
            say('landing   still waiting after %ds of %ds: %s' % (waited, reader_.wait_s, holder))
            noted += every_s
