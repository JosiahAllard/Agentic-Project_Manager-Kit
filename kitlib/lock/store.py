"""The lock's store: a folder of JSON records, read and written under
named Win32 mutexes, plus each run's liveness mutex.

    active/<run>.json      a live run's record (.stop: a stop request)
    ended/<id>.json        ended runs and holds, kept ENDED_KEEP_S
    logs/<run>.log         a detached run's output
    history/YYYY-MM.jsonl  each ended run's measured use
    queue.json             the allocation (kitlib.lock.queue)
    holds/<id>.json        owner, agent and game holds (kitlib.lock.holds)
    machine.json           the owner's division (kitlib.lock.layout)
    layout.json            the layout in force (adopted only while idle)
    games.json             game mode's list (kitlib.lock.game)
    background/<run>.d     a background run's control folder
    revision               a counter every write bumps (kitlib.lock.api.wait_change)

Store(root) is the machine's store when root is None; any other root is a scratch store with
mutexes of its own (tests). Timings live in Settings, so a test shortens them by parameter, not
by environment (the old MACHINE_LOCK_TEST_* seams). Nothing here prints: `say` is the caller's.

Under the kit's tests the machine's store is fenced (fence; docs/test-tools.md "Home fence"): it
can be built and its root named, but Store.path and Store.mutex_name raise HomeFenced, so nothing
under it is read, written or locked. A scratch store is not fenced, and a real run never is.
"""
import contextlib
import ctypes
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from ctypes import wintypes

from kitlib.host import registry
from kitlib.proc import process
from kitlib.proc import win32
from kitlib.proc.win32 import K32, ADV

# Exit codes (stable): the CLI returns LockError.code.
EXIT_FAIL, EXIT_USAGE, EXIT_BUSY, EXIT_TIMEOUT = 1, 2, 6, 8
EXIT_REQUIRE, EXIT_RESERVED, EXIT_HOLD = 10, 11, 12

WAIT_OBJECT_0, WAIT_ABANDONED_0 = 0, 0x80
# An elevated creator's default DACL shuts non-elevated callers out of the mutex. This one lets
# SYSTEM, administrators and every signed-in user open it, at medium integrity.
MUTEX_SDDL = "D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;AU)S:(ML;;NW;;;ME)"
ID_RE = re.compile(r"^[rh]-\d{8}T\d{6}Z-[0-9a-f]{4,6}$")


class LockError(Exception):
    """A refusal or failure with the CLI's exit code (USAGE 2, BUSY 6, TIMEOUT 8, REQUIRE 10,
    RESERVED 11, HOLD 12, FAIL 1) and the line that explains it."""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code, self.message = code, message


class Settings:
    """Every timing the lock uses, in seconds unless named otherwise. Settings(**overrides)
    changes some (tests); an unknown name raises TypeError. `pauses` maps a named point to
    seconds a test wants the code to stop there (Store.pause)."""
    DEFAULTS = {
        "queue_poll_s": 0.25, "queue_read_s": 2.0, "queue_write_s": 30.0, "queue_lock_s": 10.0,
        "store_lock_s": 10.0, "usage_sample_s": 5.0, "ended_keep_s": 3 * 86400,
        "tmp_keep_s": 60.0, "history_keep_days": 90, "history_min_runs": 3,
        "history_min_s": 60, "max_span_s": 10 * 365 * 86400, "hold_grace_s": 600,
        "hold_max_s": 12 * 3600, "hold_ahead_s": 7 * 86400, "on_release_s": 60.0,
        "stop_s": 60.0, "warn_scale": 1.0, "bg_stale_s": 120.0, "bg_beat_s": 3.0,
        "bg_resume_s": 180.0, "bg_free_s": 30.0, "bg_keep_s": 600.0, "bg_stop_grace_s": 50.0,
        "bg_yield_s": 120.0, "bg_yield_margin_s": 15.0, "game_poll_s": 5.0, "game_roll_s": 120.0,
        "gpu_use_cache_s": 5.0, "wait_change_poll_s": 0.25, "child_poll_s": 1.0,
    }

    def __init__(self, pauses=None, **overrides):
        bad = sorted(set(overrides) - set(self.DEFAULTS))
        if bad:
            raise TypeError("unknown lock settings: %s" % ", ".join(bad))
        for k, v in self.DEFAULTS.items():
            setattr(self, k, overrides.get(k, v))
        self.pauses = dict(pauses or {})


def profile_dir():
    """The user's profile folder from Windows (FOLDERID_Profile), never from the environment: a
    caller's USERPROFILE or HOME cannot move the store."""
    class GUID(ctypes.Structure):
        _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                    ("d4", ctypes.c_ubyte * 8)]
    folder = GUID(0x5E6C858F, 0x0E22, 0x4760,
                  (ctypes.c_ubyte * 8)(0x9A, 0xFE, 0xEA, 0x33, 0x17, 0xB6, 0x71, 0x73))
    get = ctypes.WinDLL("shell32").SHGetKnownFolderPath
    get.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                    ctypes.POINTER(ctypes.c_wchar_p)]
    get.restype = ctypes.c_long
    free = ctypes.WinDLL("ole32").CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    path = ctypes.c_wchar_p()
    hr = get(ctypes.byref(folder), 0, None, ctypes.byref(path))
    try:
        if hr != 0 or not path.value:
            raise OSError("SHGetKnownFolderPath(FOLDERID_Profile) failed: 0x%08X" % (hr & 0xFFFFFFFF))
        return path.value
    finally:
        free(path)


def real_root():
    """The machine's store: machine-lock in the kit's per-user state folder (registry.state_path;
    ~/.claude/machine-lock, where it has always been)."""
    return registry.state_path(profile_dir(), "machine-lock")


FENCED = ("the machine's lock store is refused under the kit's tests (asked for: %s): this "
          "process was fenced by the tests package or carries KIT_HOME_FENCE; a test uses a "
          "scratch store, Store(root=<a temp folder>) (docs/test-tools.md \"Home fence\")")


def fence(what):
    """Refuse the machine's lock store to a process under the kit's tests: HomeFenced, naming
    `what` (what was asked for), when kitlib.config.user.fenced(). A BaseException, so neither the
    store's `except OSError` nor a caller's `except Exception` turns the refusal into a default or
    a warning. A real run falls through: one global read and one environment read."""
    from kitlib.config import user
    if user.fenced():
        raise user.HomeFenced(FENCED % what)


def machine_home():
    """The home folder the machine's lock store lives under, for what is read beside it (the
    machine config's lock settings, kitlib.lock.site): profile_dir(), refused under the kit's
    tests (fence)."""
    fence("the store's home folder")
    return profile_dir()


_SA = []


def mutex_sa():
    if not _SA:
        psd = wintypes.LPVOID()
        if not ADV.ConvertStringSecurityDescriptorToSecurityDescriptorW(MUTEX_SDDL, 1,
                                                                        ctypes.byref(psd), None):
            raise ctypes.WinError(ctypes.get_last_error())
        _SA.append(win32.SECURITY_ATTRIBUTES(ctypes.sizeof(win32.SECURITY_ATTRIBUTES), psd, False))
    return _SA[0]


# ------------------------------------------------------------------ time and ids
def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(t=None):
    return (t or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def epoch(s):
    """Seconds since 1970 of an ISO time, or None."""
    try:
        return parse_iso(s).timestamp()
    except (TypeError, ValueError):
        return None


def new_id(prefix):
    """'r-20261006T120000Z-abcdef' (a run) or 'h-20261006T120000Z-abcd' (a hold)."""
    return "%s-%s-%s" % (prefix, utc_now().strftime("%Y%m%dT%H%M%SZ"),
                         secrets.token_hex(3 if prefix == "r" else 2))


def local_text(t, ref=None):
    """'14:00' (today) or '2026-10-05 14:00', local time; t a datetime or epoch seconds."""
    if not isinstance(t, datetime.datetime):
        t = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    lt = t.astimezone()
    today = (ref or utc_now()).astimezone().date()
    return lt.strftime("%H:%M") if lt.date() == today else lt.strftime("%Y-%m-%d %H:%M")


def local_ok(*times):
    """Raises (OverflowError or OSError) unless each time, plus a day, has a local time."""
    for t in times:
        (t + datetime.timedelta(days=1)).astimezone()


# ------------------------------------------------------------------ who is asking
# An actor's agent host [pid, ctime] is under HOST_KEY. Actors made before the key was neutral
# carry it under LEGACY_HOST_KEY, which is read too (actor_host) and never written.
HOST_KEY = "host"
LEGACY_HOST_KEY = "claude"


def agent_hosts(registry_path=None):
    """The lower-case image names of the processes that host agents: every registered host's
    profile process_names (hosts.json; Claude Code runs as claude.exe, or node.exe when installed
    from npm, and so does its desktop app's terminal panel; Codex as codex.exe). A hosts.json
    that cannot be read raises RegistryError, which each caller here treats as an agent (fail
    closed)."""
    names = registry.union("process_names", registry_path)
    return frozenset(n.lower() for n in names)


def session_of(env, registry_path=None):
    """The agent session id `env` carries (shown, never trusted): the first set one of the
    registered hosts' profile session_env variables, else None, as it is when hosts.json cannot
    be read."""
    try:
        names = registry.union("session_env", registry_path)
    except registry.RegistryError:
        return None
    return next((env.get(n) for n in names if env.get(n)), None)


def actor_host(actor):
    """The actor's agent host [pid, ctime] as it carries it: under HOST_KEY, else under
    LEGACY_HOST_KEY (an actor made before the rename); None when neither is set."""
    if not isinstance(actor, dict):
        return None
    return actor.get(HOST_KEY) or actor.get(LEGACY_HOST_KEY)


def chain_host(chain, names, hosts=None):
    """The nearest agent host [pid, ctime] in chain ([(pid, ctime)], nearest first) by exe name
    (names {pid: exe}; hosts the agent images, default agent_hosts()), or None. A pid with no
    name (a parent that has exited but is still open somewhere) is not a host."""
    hosts = agent_hosts() if hosts is None else hosts
    for pid, ct in chain:
        if (names.get(pid) or "").lower() in hosts:
            return [int(pid), int(ct)]
    return None


def caller(name=None, env=None):
    """Who is asking, from this process's tree, never from what a caller says: {role:
    owner | agent, host: [pid, ctime] of the nearest agent host above this process or None,
    ancestors: [[pid, ctime]], session: the host's session id (session_of; shown, never trusted),
    name}. The role is agent when any ancestor is an agent host (agent_hosts), whatever the
    environment says, and when the tree or the host registry cannot be read (fail closed)."""
    env = os.environ if env is None else env
    try:
        snap = process.snapshot()
        chain = process.ancestors_of(os.getpid(), process.own_ctime(),
                                     {p: pp for p, pp, _n in snap})
        host = chain_host(chain, {p: n for p, _pp, n in snap})
        role = "agent" if host or not chain else "owner"
    except Exception:
        chain, host, role = [], None, "agent"
    return {"role": role, HOST_KEY: host, "ancestors": sorted([p, c] for p, c in chain),
            "session": session_of(env), "name": name}


def tree_role(pairs):
    """owner or agent for a run started under the processes pairs ([pid, ctime], a detached
    run's launcher ancestry; for holds.admits): agent when any is a live agent host, when any can no
    longer be named (it exited, or its pid now names another process), when there are none, or
    when the processes or the host registry cannot be read (fail closed)."""
    try:
        names = {p: n for p, _pp, n in process.snapshot()}
        if not pairs:
            return "agent"
        hosts = agent_hosts()
        for pid, ct in pairs:
            n = names.get(int(pid))
            if n is None or process.proc_ctime(int(pid)) != int(ct):
                return "agent"
            if n.lower() in hosts:
                return "agent"
        return "owner"
    except Exception:
        return "agent"


def owns(actor, rec):
    """Whether record rec (a run or a hold) is the actor's: the actor's nearest agent host
    (actor_host; read from its tree by caller()) is among rec's recorded ancestors, so the record
    was made inside that session (its subagents run in the same process). A session id, which
    anyone can copy from snapshot(), grants nothing."""
    host = actor_host(actor)
    if not (isinstance(host, (list, tuple)) and len(host) == 2):
        return False
    return [int(host[0]), int(host[1])] in [list(a) for a in rec.get("ancestors") or []]


# ------------------------------------------------------------------ files
def write_json(path, data, replace_s=2.0):
    """Writes a temp file and replaces `path` with it, retrying os.replace for replace_s while a
    reader has `path` open, then PermissionError."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=1)
    end = time.monotonic() + replace_s
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if time.monotonic() >= end:
                with contextlib.suppress(OSError):
                    os.remove(tmp)
                raise
            time.sleep(0.05)


def read_json(path):
    """A JSON object from path, or None (missing, unreadable or not an object)."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def read_record(path, wait_s=0.0):
    """(record, there): the JSON object at path, and whether its file is there. (None, False):
    no such file. (None, True): there, but not read: an open refused, or not a JSON object. A
    record is written by os.replace (write_json), and on Windows an open that lands while it
    swaps the file is refused (PermissionError), never answered with no such file: that is "not
    now", never "no record". Such a file is read again every 0.1 s for wait_s (0: once)."""
    end = time.monotonic() + wait_s
    while True:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d, True
        except FileNotFoundError:
            return None, False
        except (OSError, ValueError):
            pass
        if time.monotonic() >= end:
            return None, True
        time.sleep(0.1)


def remove(path):
    with contextlib.suppress(OSError):
        os.remove(path)


def public(d):
    """A record without its computed `_` keys."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


# ------------------------------------------------------------------ the run mutex
class RunMutex:
    """A run's liveness signal: mutex run.<id>, created owned by a thread of its own, which keeps
    it until release(). Windows abandons it when the wrapper dies, so the next look at the queue
    drops a dead caller's entry. Raises OSError when it cannot be had."""

    def __init__(self, name):
        self.name = name
        self.error = None
        self.ready, self.end, self.done = threading.Event(), threading.Event(), threading.Event()
        threading.Thread(target=self._run, args=(mutex_sa(),), daemon=True).start()
        self.ready.wait()
        if self.error:
            raise OSError(self.error)

    def _run(self, sa):
        h = None
        try:
            h = K32.CreateMutexW(ctypes.byref(sa), True, self.name)
            if not h:
                self.error = "CreateMutex %s: windows error %d" % (self.name, ctypes.get_last_error())
                return
            if (ctypes.get_last_error() == win32.ERROR_ALREADY_EXISTS
                    and K32.WaitForSingleObject(h, 0) not in (WAIT_OBJECT_0, WAIT_ABANDONED_0)):
                self.error = "the run mutex %s is owned by another process" % self.name
                return
            self.ready.set()
            self.end.wait()
            K32.ReleaseMutex(h)
        finally:
            if h:
                K32.CloseHandle(h)
            self.ready.set()
            self.done.set()

    def release(self):
        self.end.set()
        self.done.wait(10)


# ------------------------------------------------------------------ the store
class Store:
    """One lock store. root None: the machine's (~/.claude/machine-lock, from Windows);
    else a scratch store whose mutexes are named after its path. say(text): where the library's
    progress lines go (default: nowhere; the CLI prints them). Under the kit's tests the machine's
    store gives no file path and names no mutex: path and mutex_name raise HomeFenced (fence)."""

    def __init__(self, root=None, settings=None, say=None, hardware=None, kinds_path=None):
        real = real_root()
        self.root = os.path.abspath(root or real)
        self.testing = os.path.normcase(self.root) != os.path.normcase(real)
        self.settings = settings or Settings()
        self.say = say or (lambda text: None)
        self.hardware = hardware            # kitlib.lock.layout: None = detect this machine
        self.kinds_path = kinds_path        # kitlib.lock.kinds: None = the kit's kinds.json
        self._gpu_cache = None
        self._game_at = 0.0

    def path(self, *parts):
        if not self.testing:
            fence("a file path")
        return os.path.join(self.root, *parts)

    def mutex_name(self, lock):
        if not self.testing:
            fence("a mutex name")
            return "Global\\MachineLock." + lock
        h = hashlib.sha1(os.path.normcase(self.root).encode("utf-8")).hexdigest()[:12]
        return "Global\\MachineLock.test-%s.%s" % (h, lock)

    def pause(self, name):
        """Tests only: stop at a named point for settings.pauses[name] seconds, with
        <name>.paused written first so the test knows it is there."""
        s = self.settings.pauses.get(name)
        if s:
            with open(self.path(name + ".paused"), "w") as f:
                f.write("x")
            time.sleep(s)

    @contextlib.contextmanager
    def lock(self, name, wait_s=None):
        """A named store mutex, held while its records are read and written. Raises OSError when
        it is not had within wait_s (default settings.store_lock_s). Recursive for its thread."""
        wait_s = self.settings.store_lock_s if wait_s is None else wait_s
        h = K32.CreateMutexW(ctypes.byref(mutex_sa()), False, self.mutex_name(name))
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if K32.WaitForSingleObject(h, int(wait_s * 1000)) not in (WAIT_OBJECT_0, WAIT_ABANDONED_0):
                raise OSError("the %s mutex was not free within %g s" % (name, wait_s))
            try:
                yield
            finally:
                K32.ReleaseMutex(h)
        finally:
            K32.CloseHandle(h)

    def run_mutex(self, rid):
        return RunMutex(self.mutex_name("run." + rid))

    def run_dead(self, e):
        """True when entry or record e's wrapper is surely gone: its pid and creation time name
        no live process, or its run mutex is gone, abandoned or unowned. A mutex this process may
        not open leaves it to the pid and creation time."""
        try:
            if not process.alive(int(e.get("pid") or 0), e.get("ctime")):
                return True
        except (TypeError, ValueError):
            return True
        h = K32.OpenMutexW(win32.SYNCHRONIZE | win32.MUTEX_MODIFY_STATE, False,
                           self.mutex_name("run." + str(e["id"])))
        if not h:
            return ctypes.get_last_error() != win32.ERROR_ACCESS_DENIED
        try:
            if K32.WaitForSingleObject(h, 0) in (WAIT_OBJECT_0, WAIT_ABANDONED_0):
                K32.ReleaseMutex(h)
                return True
            return False
        finally:
            K32.CloseHandle(h)

    # -------------------------------------------------------------- records
    def write(self, rel, data, replace_s=2.0):
        """write_json under the root, then a revision bump (api.wait_change wakes)."""
        write_json(self.path(*rel.split("/")), data, replace_s)
        self.bump()

    def read(self, rel):
        return read_json(self.path(*rel.split("/")))

    def drop(self, rel):
        p = self.path(*rel.split("/"))
        if os.path.exists(p):
            remove(p)
            self.bump()

    def revision(self):
        try:
            with open(self.path("revision"), encoding="utf-8") as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def bump(self):
        """revision + 1, under the revision mutex. A bump that cannot be had is skipped: the
        next write bumps, and wait_change also wakes on its timeout."""
        try:
            with self.lock("revision", 2):
                n = self.revision() + 1
                os.makedirs(self.root, exist_ok=True)
                tmp = self.path("revision.%d.tmp" % os.getpid())
                with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                    f.write(str(n))
                os.replace(tmp, self.path("revision"))
        except OSError:
            pass

    def live_runs(self, prune=False):
        """Run records whose wrapper (pid and creation time) is alive. prune: delete the others,
        orphaned .stop and .giveback files (kitlib.lock.giveback) and dead writers' .tmp files."""
        out, folder = [], self.path("active")
        try:
            names = sorted(os.listdir(folder))
        except FileNotFoundError:
            return out
        for n in names:
            p = os.path.join(folder, n)
            if not n.endswith(".json"):
                if prune and n.endswith(".stop") and not os.path.exists(p[:-5] + ".json"):
                    remove(p)
                elif prune and n.endswith(".giveback") and not os.path.exists(p[:-9] + ".json"):
                    remove(p)
                elif prune and n.endswith(".tmp"):
                    with contextlib.suppress(OSError):
                        if time.time() - os.path.getmtime(p) > self.settings.tmp_keep_s:
                            remove(p)
                continue
            d = read_json(p)
            if d is None:
                continue
            if process.alive(d.get("pid", 0), d.get("ctime")):
                out.append(d)
            elif prune:
                remove(p)
                remove(p[:-5] + ".stop")
                remove(p[:-5] + ".giveback")
                self.bump()
        return out

    def prune_files(self, live_ids):
        """Ended records and logs past ENDED_KEEP_S, history months past history_keep_days,
        and ended background folders past bg_keep_s."""
        now, s = time.time(), self.settings
        for sub in ("ended", "logs"):
            with contextlib.suppress(OSError):
                for n in os.listdir(self.path(sub)):
                    if now - os.path.getmtime(self.path(sub, n)) > s.ended_keep_s:
                        remove(self.path(sub, n))
        with contextlib.suppress(OSError, ValueError):
            for n in os.listdir(self.path("history")):
                m = re.fullmatch(r"(\d{4})-(\d{2})\.jsonl", n)
                if m:
                    y, mo = int(m.group(1)), int(m.group(2))
                    month_end = datetime.datetime(y + mo // 12, mo % 12 + 1, 1,
                                                  tzinfo=datetime.timezone.utc)
                    if (utc_now() - month_end).days > s.history_keep_days:
                        remove(self.path("history", n))
        with contextlib.suppress(OSError):
            for n in os.listdir(self.path("background")):
                d = self.path("background", n)
                if (n.endswith(".d") and n[:-2] not in live_ids and os.path.isdir(d)
                        and now - os.path.getmtime(d) > s.bg_keep_s):
                    shutil.rmtree(d, ignore_errors=True)

    # -------------------------------------------------------------- history
    HISTORY_KEYS = ("id", "repo", "slug", "kind", "need", "cores", "gpus", "expect_s",
                    "expect_from", "since", "held_since", "ended", "code", "how", "cpu_s",
                    "peak_cpu_cores", "peak_mem_mb", "peak_commit_mb", "hold", "parent",
                    "pinned", "given_back", "tree")
    DOCKER_EXEC_RE = re.compile(r"\bdocker(\.exe)?\b.*\bexec\b", re.I)

    def history_append(self, d):
        """Appends ended run record d's history line to history/YYYY-MM.jsonl, the month it
        ended. A run that never held anything (refused, or it gave up waiting) has none. Each
        line adds held_s, wait_s, partial (the command runs docker exec, so its use is partly
        outside the job) and background."""
        if not d or not (d.get("held_since") or d.get("background")):
            return
        h = {k: d.get(k) for k in self.HISTORY_KEYS}
        try:
            end = parse_iso(d["ended"])
            h["held_s"] = int((end - parse_iso(d.get("held_since") or d["since"])).total_seconds())
        except (KeyError, TypeError, ValueError):
            h["held_s"] = None
        try:
            h["wait_s"] = int((parse_iso(d["held_since"]) - parse_iso(d["since"])).total_seconds())
        except (KeyError, TypeError, ValueError):
            h["wait_s"] = None
        h.update(partial=bool(self.DOCKER_EXEC_RE.search(" ".join(str(c) for c in d.get("cmd") or ()))),
                 background=bool(d.get("background")))
        with contextlib.suppress(OSError):
            os.makedirs(self.path("history"), exist_ok=True)
            with open(self.path("history", "%s.jsonl" % d["ended"][:7]), "a", encoding="utf-8",
                      newline="\n") as f:
                f.write(json.dumps(h, sort_keys=True) + "\n")

    def history_records(self, days=None, since=None, until=None):
        """History lines of runs that ended in [since, until) (datetimes; default the last
        `days`, else history_keep_days), oldest first; a malformed line is skipped."""
        out = []
        try:
            names = sorted(n for n in os.listdir(self.path("history"))
                           if re.fullmatch(r"\d{4}-\d{2}\.jsonl", n))
        except OSError:
            return out
        if since is None:
            since = utc_now() - datetime.timedelta(
                days=self.settings.history_keep_days if days is None else days)
        lo, hi = since.strftime("%Y-%m"), until.strftime("%Y-%m") if until else "9999-99"
        for n in names:
            if not lo <= n[:7] <= hi:
                continue
            try:
                with open(self.path("history", n), encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    d = json.loads(line)
                    t = parse_iso(d["ended"])
                    if isinstance(d.get("id"), str) and t >= since and (until is None or t < until):
                        out.append(d)
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
        out.sort(key=lambda d: d["ended"])
        return out

    def history_expect(self, repo, kind):
        """Expected seconds of a run of repo and kind: the median held time of its finished
        (how: done) runs, at least history_min_s; None with fewer than history_min_runs."""
        s = self.settings
        ds = sorted(float(d["held_s"]) for d in self.history_records()
                    if d.get("repo") == repo and d.get("kind") == kind and d.get("how") == "done"
                    and isinstance(d.get("held_s"), (int, float)) and d["held_s"] >= 0)
        if len(ds) < s.history_min_runs:
            return None
        m = ds[len(ds) // 2] if len(ds) % 2 else (ds[len(ds) // 2 - 1] + ds[len(ds) // 2]) / 2
        return max(s.history_min_s, int(round(m)))
