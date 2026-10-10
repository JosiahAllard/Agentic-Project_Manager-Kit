"""bg_client: the trainer's side of the lock's background tier, kitlib.lock.background.

Standard library only and no Windows calls, so it can be copied into a container image as it is.

    import sys, bg_client
    resume_from_newest_checkpoint()               # every start, the first and each resume
    while training:
        if bg_client.should_pause():              # at least every POLL_S, and before step one
            save_checkpoint()                     # within CHECKPOINT_S
            sys.exit(bg_client.PAUSE_EXIT)        # the wrapper starts this command again later
        train_a_little()
    sys.exit(0)                                   # done: the background run ends

`machine_lock.py background ... -- <command>` gives the command MACHINE_LOCK_BG_CONTROL, a
control.json it rewrites every few seconds: {"seq", "pause", "reason", "stale_s", ...}, and
MACHINE_LOCK_BG (<run id>:<pid>:<ctime>). Inside a persistent container that mounts the store's
background folder read-only (say at /mlock), run the trainer with
`docker exec -e MACHINE_LOCK_BG -e MACHINE_LOCK_BG_ROOT=/mlock <container> ...`: with both set,
control.json is found at $MACHINE_LOCK_BG_ROOT/<run id>.d/control.json. The launcher must stay
attached and return the trainer's exit code.

should_pause() is True when control.json says pause (foreground work or an owner or game hold took its slot),
or when its seq has not changed for stale_s seconds (at most STALE_S) on this process's own
monotonic clock: the wrapper is gone, or control.json cannot be read, so the trainer stops instead
of running unwatched. No wall clock is compared across the host and a VM. It is always True when
the environment says the run is managed but no control.json can be named (MACHINE_LOCK_BG_ROOT
without a well-formed MACHINE_LOCK_BG, or MACHINE_LOCK_BG with neither MACHINE_LOCK_BG_ROOT nor
MACHINE_LOCK_BG_CONTROL): fail closed. With none of the three set (the command was not started by
the tool) it is always False.
"""
import json
import os
import re
import time

PAUSE_EXIT = 75                 # exit code after the checkpoint (kitlib.lock.background.PAUSE_EXIT)
STALE_S = 120.0                 # the longest a heartbeat may stay unchanged (Settings.bg_stale_s)
POLL_S = 30.0                   # call should_pause() at least this often
CHECKPOINT_S = 20.0             # the checkpoint's budget once it says pause
CONTROL_ENV = "MACHINE_LOCK_BG_CONTROL"
RUN_ENV = "MACHINE_LOCK_BG"
ROOT_ENV = "MACHINE_LOCK_BG_ROOT"
_RUN_RE = re.compile(r"^(r-\d{8}T\d{6}Z-[0-9a-f]{6}):\d+:\d+$")     # MACHINE_LOCK_BG's value


def resolve(env=None):
    """(control.json path, problem). $MACHINE_LOCK_BG_ROOT/<run id>.d/control.json when
    MACHINE_LOCK_BG_ROOT is set (a container that mounts the folder), else MACHINE_LOCK_BG_CONTROL
    (a native trainer). problem is set, and the path None, when the run is managed but no path can
    be named: MACHINE_LOCK_BG_ROOT with MACHINE_LOCK_BG missing or malformed, or MACHINE_LOCK_BG
    with no way to the file. (None, None) when nothing is set: not started by machine_lock."""
    env = os.environ if env is None else env
    root, run, ctl = env.get(ROOT_ENV), env.get(RUN_ENV), env.get(CONTROL_ENV)
    if root:
        m = _RUN_RE.match(run or "")
        if not m:
            return None, ("%s is set but %s is %s" % (ROOT_ENV, RUN_ENV, "missing" if not run
                                                       else "malformed (%r)" % run))
        return os.path.join(root, m.group(1) + ".d", "control.json"), None
    if ctl:
        return ctl, None
    if run:
        return None, "%s is set but neither %s nor %s is" % (RUN_ENV, ROOT_ENV, CONTROL_ENV)
    return None, None


def control_path(env=None):
    """resolve()'s path: None when not managed, or when managed but unnamable."""
    return resolve(env)[0]


class Control:
    """One reader of control.json. `clock` is a monotonic clock in seconds (tests pass their own).
    `env` defaults to os.environ; an explicit `path` skips it."""

    def __init__(self, path=None, clock=time.monotonic, env=None):
        self.path, self.problem = (path, None) if path is not None else resolve(env)
        self.clock = clock
        self.seq = None
        self.changed = clock()
        self.stale_s = STALE_S
        self.reason = None

    @property
    def managed(self):
        """True when started by machine_lock (a control path, or a problem naming it)."""
        return bool(self.path or self.problem)

    def should_pause(self):
        if self.problem:                # managed, but nothing to read: fail closed
            self.reason = self.problem
            return True
        if not self.path:
            return False
        now = self.clock()
        try:
            with open(self.path, "rb") as f:
                d = json.loads(f.read().decode("utf-8-sig"))
            seq, pause, stale = d["seq"], d.get("pause") is True, d.get("stale_s")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            d = None                    # being replaced this instant, or gone: no news
        if d is not None:
            if isinstance(stale, (int, float)) and not isinstance(stale, bool) and stale > 0:
                self.stale_s = min(float(stale), STALE_S)
            if seq != self.seq:
                self.seq, self.changed = seq, now
            if pause:
                self.reason = d.get("reason") or "pause asked"
                return True
        if now - self.changed >= self.stale_s:
            self.reason = ("%s has not changed for %.0f s: the machine_lock wrapper is gone"
                           % (self.path, now - self.changed))
            return True
        return False


_DEFAULT = []


def _default():
    if not _DEFAULT:
        _DEFAULT.append(Control())
    return _DEFAULT[0]


def should_pause():
    """True when the trainer should checkpoint now and exit PAUSE_EXIT."""
    return _default().should_pause()


def reason():
    """Why the last True from should_pause(), or None."""
    return _default().reason


def managed():
    """True when this process was started by `machine_lock.py background`."""
    return _default().managed
