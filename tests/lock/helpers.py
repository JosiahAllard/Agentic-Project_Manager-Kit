"""Shared fakes for the lock tests: a scratch store (its own folder and mutex names) on fake
hardware (kitlib.lock.layout.fake_hardware), its own kinds table, and game probes that never look
at this machine's processes. Settings are shortened by parameter (store.Settings), never by
environment."""
import contextlib
import ctypes
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from ctypes import wintypes

from kitlib.host import registry
from tests.platforms import windows_only

if os.name == "nt":
    from kitlib.lock import holds as hl
    from kitlib.lock import layout as lay
    from kitlib.lock.store import Settings, Store, iso, utc_now
    from kitlib.proc import process, win32

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KINDS = {"kinds": {
    "engine-test": {"cpu": "medium", "gpu": 0, "warn_min": 30},
    "test": {"cpu": "medium", "gpu": 0, "warn_min": 30},
    "cpu-eval": {"cpu": "heavy", "gpu": 0, "warn_min": 60},
    "local-ai": {"cpu": "medium", "gpu": "pool", "share": True, "warn_min": 30},
    "measure": {"cpu": "heavy", "gpu": "pool", "share": False, "warn_min": 60},
}}
FAST = {"wait_change_poll_s": 0.05, "queue_read_s": 0.3, "queue_poll_s": 0.05,
        "store_lock_s": 5.0, "queue_lock_s": 5.0, "usage_sample_s": 0.5, "child_poll_s": 0.1}
OWNER = {"role": "owner", "session": None, "host": None, "ancestors": [], "name": "test-owner"}


@contextlib.contextmanager
def unopenable(path):
    """While inside, every other open of path fails with PermissionError: a handle on it that
    shares nothing. It is how a read fails when it lands while a writer's os.replace swaps the
    file (an open then is refused, never answered with no such file)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = k32.CreateFileW
    create.restype = wintypes.HANDLE
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    h = create(path, 0x80000000, 0, None, 3, 0, None)       # GENERIC_READ, no sharing, OPEN_EXISTING
    if h is None or h == win32.INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        win32.K32.CloseHandle(h)


def claude_of(session):
    """A made-up agent host [pid, ctime] per session name (a scratch store takes actors as given,
    kitlib.lock.api.who)."""
    return [900000 + sum(map(ord, session)) % 9999, 1]


def agent(session):
    """The actor a session's agent is on a scratch store: its host is among its ancestors, so the
    holds it makes are its own (kitlib.lock.store.owns)."""
    c = claude_of(session)
    return {"role": "agent", "session": session, "host": c, "ancestors": [c],
            "name": "test-agent"}


def own_affinity():
    """The set of thread ids (group 0) this process may run on. Under the machine lock (the gate
    runs the suite as a run) it is the outer run's cores, and a job nested in that run's job
    cannot run anywhere else."""
    h = process.open_process(os.getpid())
    try:
        pm, sm = ctypes.c_size_t(), ctypes.c_size_t()
        if not win32.K32.GetProcessAffinityMask(h, ctypes.byref(pm), ctypes.byref(sm)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        process.close(h)
    return {b for b in range(64) if pm.value >> b & 1}


def under_agent_host(tmp, argv, timeout=120):
    """(exit code, stdout, stderr) of argv run as the child of a process named claude.exe (a copy
    of cmd.exe in tmp), with clean_env(): the caller check (kitlib.lock.store.caller) sees an agent
    host above it whoever runs the suite. argv must hold no cmd.exe metacharacters."""
    fake = os.path.join(tmp, "claude.exe")
    if not os.path.exists(fake):
        shutil.copy(os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe"), fake)
    line = '"%s" /d /s /c "%s"' % (fake, subprocess.list2cmdline(argv))
    r = subprocess.run(line, env=clean_env(), capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def clean_env(**extra):
    """Inherited environment without registered host sessions and machine lock markers."""
    sessions = {name.upper() for name in registry.union('session_env')} | {"CLAUDE_PID"}
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in sessions and not k.upper().startswith("MACHINE_LOCK_")}
    env.update(extra)
    return env


@windows_only("the lock is Windows only")
class StoreCase(unittest.TestCase):
    """setUp makes self.store: a scratch Store on fake_hardware(**HW) with KINDS and FAST
    settings (plus SETTINGS), and game probes: no process runs (self.procs), no anti-cheat service,
    and pins recorded in self.pins."""
    HW = {"threads": 24, "smt": 2, "l3": 2}
    SETTINGS = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lock-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.kinds_path = os.path.join(self.tmp, "kinds.json")
        with open(self.kinds_path, "w", encoding="utf-8") as f:
            json.dump(KINDS, f)
        self.hw = lay.fake_hardware(**self.HW)
        self.root = os.path.join(self.tmp, "store")
        self.store = self.make_store()
        self.procs, self.pins, self.unpins = [], [], []
        self.masks = {}                     # pid -> the affinity a fake pin set

    def make_store(self, **settings):
        s = Store(root=self.root, settings=Settings(**dict(FAST, **dict(self.SETTINGS, **settings))),
                  hardware=self.hw, kinds_path=self.kinds_path)
        s.game_probes = {"procs": lambda: list(self.procs), "services": lambda: [],
                         "pin": self.fake_pin, "unpin": self.fake_unpin,
                         "ctime": lambda pid: 1000 + pid,
                         "affinity": lambda pid, ct: self.masks.get(pid, self.FIRST_MASK)}
        return s

    FIRST_MASK = 0xFFFF                     # a fake game's affinity before any pin

    def fake_pin(self, pid, mask):
        self.pins.append((pid, mask))
        self.masks[pid] = mask
        return "pinned"

    def fake_unpin(self, pid, g):
        self.unpins.append(pid)
        return "restored"

    def layout(self):
        return lay.in_force(self.store)["layout"]

    def confine(self, slots_needed):
        """Keeps runs on this store to fake cpu slots whose threads this process may run on (an
        owner slots hold over the rest), so a run's job pins inside the gate's own run. Skips the
        test when fewer than slots_needed such slots exist. Returns the usable slots."""
        L = self.layout()
        own = own_affinity()
        ok = [i for i, ts in enumerate(L["slots_cpu"]) if set(ts) <= own]
        if len(ok) < slots_needed:
            self.skipTest("this process may run on only %d of the fake cpu slots" % len(ok))
        rest = [i for i in range(L["cpu_slots"]) if i not in ok]
        if rest:
            now = utc_now()
            hl.create(self.store, L, {"owner": "owner", "scope": "slots", "start": now,
                                      "end": now + datetime.timedelta(hours=1),
                                      "slots": {"cpu": rest, "gpu": []}}, OWNER, None)
        return ok

    def live_record(self, rid, **fields):
        """A live run record for rid whose wrapper is this test process (so it stays live)."""
        rec = {"id": rid, "pid": os.getpid(), "ctime": process.own_ctime(), "state": "held",
               "repo": "kit", "slug": "t", "kind": "engine-test", "since": iso(),
               "held_since": iso(), "expect_s": 600, "session": None, "ancestors": [],
               "cores": [], "gpus": []}
        rec.update(fields)
        self.store.write("active/%s.json" % rid, rec)
        return rec
