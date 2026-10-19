"""Watch for console windows that open while kit code runs: Monitor records each one, so a gate or a
machine-lock run can say how many windows it opened. Windows only, standard library and ctypes only.

How it watches. A daemon thread installs two out-of-context WinEvent hooks (SetWinEventHook with
WINEVENT_OUTOFCONTEXT, one for EVENT_OBJECT_SHOW and one for EVENT_OBJECT_NAMECHANGE, so the noisy
events between them are not delivered) and pumps messages (GetMessageW) until stop() posts WM_QUIT
to it (PostThreadMessageW); the hooks are then removed (UnhookWinEvent) on that same thread. The
callbacks run on that thread, never inside another process.

What it records (Window), all top-level windows:
- A `ConsoleWindowClass` window (the classic console host, conhost.exe) shown after start. Its
  owning pid is what GetWindowThreadProcessId gives, which for a console window is the console's
  client process, not conhost (the host is read from the event's thread instead).
- A `CASCADIA_HOSTING_WINDOW_CLASS` window (Windows Terminal) shown after start: where a console
  program opens when Windows Terminal is the default terminal. The window belongs to
  WindowsTerminal.exe, so the program is found by correlation (below).
- A title change of a Terminal window that existed before start, when the new title looks like a
  launched program (looks_launched): how a new tab in an open Terminal window shows. It is kept
  only when correlation finds the program (below); else it is taken for a tab switch and dropped.
Windows that existed before start are ignored, whatever they do later. The two classes are WATCHED,
the default; Monitor(classes=...) records any other class as it records a console window (a test
passes a class of its own).

Correlation. When a console program starts under Windows Terminal, its console host (OpenConsole or
conhost) shows a `PseudoConsoleWindow` whose owning pid, as GetWindowThreadProcessId gives it, is
the program's; the Terminal window or tab is then retitled to the program's path. attribute() pairs
each Terminal window or tab with an unused PseudoConsoleWindow record within CORRELATE_S seconds,
one whose exe is the one the title names first, then (a window only) the nearest in time. Pseudo
records themselves are not counted: a program started with no window (CREATE_NO_WINDOW) makes none.
A window whose program is a console host (conhost, OpenConsole) within CORRELATE_S of a window of a
known program with the same ours (any ours, when the host window is not counted) is taken for that
program's twin and dropped (_drop_twins), so another run's window never hides one of this run's.

Whose window. `ours(pid)` (Monitor's argument, called on the monitor thread) says whether a pid is
the caller's: True, False, or None for unknown. A machine-lock run passes its job's membership
(kitlib.proc.job.Job.contains). A window whose program is known not to be the caller's is listed
apart and not counted (report_lines). One whose program cannot be placed (it has exited by the time
the event is read, or cannot be opened) is counted, tagged `run unknown`: so another program's
short-lived window can count against a run.

The title heuristic (looks_launched) and its limits:
- A title counts as launched when, after an "Administrator: " prefix, it is a full path to a
  .exe, .com, .bat or .cmd file, or a bare `name.exe` (or .com, .bat, .cmd), quoted or not, with or
  without arguments after it; cmd's "<windir>\\system32\\cmd.exe - command" titles are of that kind.
- A program that sets its own title before the event is read (PowerShell, many TUIs) is not seen
  by its title: a new tab of one is missed unless the title change still matches, and a new
  Terminal window of one is still counted (by its show event) but its title is whatever it set.
- A user's own tab whose title is a path (cmd.exe) and a tab switch to it make a title change that
  looks launched: kept only when a PseudoConsoleWindow record falls within CORRELATE_S, so a tab
  switch close in time to a real program start can be misread as a tab.
- Events are read after the fact: a window shown and gone before the callback runs cannot be read.
  One whose event thread belongs to a console host (conhost.exe or OpenConsole.exe) is counted
  as `gone` (a separate line), not as a window: its class and program are not known.
- The tail: stop() waits until no event has arrived for QUIET_S, at most DRAIN_S, before it ends the
  thread, so the events of a window shown just before stop() are read. An event Windows queues
  later than that (a busy desktop that keeps events coming for all of DRAIN_S) is missed.
- The hooks are system-wide in this desktop session: other sessions' and the user's own windows
  are seen too, which is why `ours` exists. A process with no interactive desktop (session 0, a
  service) has no windows to see: the monitor then records nothing and cannot tell.
- A window handle is compared by value: a pre-start window destroyed and its handle reused by a
  new one in the same run would be missed (Windows makes this rare).
"""
import ctypes
import os
import re
import threading
import time
from ctypes import wintypes

from kitlib.proc import win32

CONSOLE = "ConsoleWindowClass"
TERMINAL = "CASCADIA_HOSTING_WINDOW_CLASS"
PSEUDO = "PseudoConsoleWindow"
HOSTS = ("conhost.exe", "openconsole.exe")
WATCHED = (CONSOLE, TERMINAL)   # Monitor's classes: the windows it records

EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0
OBJID_WINDOW = 0
CHILDID_SELF = 0
WM_QUIT = 0x0012
PM_NOREMOVE = 0
THREAD_QUERY_LIMITED_INFORMATION = 0x0800

CORRELATE_S = 5.0       # a Terminal window or tab and its program's PseudoConsoleWindow
START_WAIT_S = 5.0      # start(): the thread's hooks are in, or the monitor gives up
STOP_WAIT_S = 5.0       # stop(): the thread has ended, or the monitor says it did not
QUIET_S = 0.05          # stop(): no event for this long and the queue is taken as drained
DRAIN_S = 0.2           # stop(): the most it waits for that quiet before it ends the thread
RECORD_CAP = 50         # records(): the most windows a record keeps

WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                  wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_API = []


class _Api:
    """Private user32 and kernel32 instances, so no prototype here touches win32.K32's."""

    def __init__(self):
        u, k = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32",
                                                                            use_last_error=True)
        H, DW, BOOL, HWND = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.HWND
        P_MSG = ctypes.POINTER(wintypes.MSG)
        for f, r, a in (
                (u.SetWinEventHook, H, (DW, DW, wintypes.HMODULE, WINEVENTPROC, DW, DW, DW)),
                (u.UnhookWinEvent, BOOL, (H,)),
                (u.GetMessageW, BOOL, (P_MSG, HWND, wintypes.UINT, wintypes.UINT)),
                (u.PeekMessageW, BOOL, (P_MSG, HWND, wintypes.UINT, wintypes.UINT,
                                        wintypes.UINT)),
                (u.TranslateMessage, BOOL, (P_MSG,)),
                (u.DispatchMessageW, wintypes.LPARAM, (P_MSG,)),
                (u.PostThreadMessageW, BOOL, (DW, wintypes.UINT, wintypes.WPARAM,
                                              wintypes.LPARAM)),
                (u.EnumWindows, BOOL, (WNDENUMPROC, wintypes.LPARAM)),
                (u.GetClassNameW, ctypes.c_int, (HWND, wintypes.LPWSTR, ctypes.c_int)),
                (u.GetWindowTextW, ctypes.c_int, (HWND, wintypes.LPWSTR, ctypes.c_int)),
                (u.GetWindowThreadProcessId, DW, (HWND, ctypes.POINTER(DW))),
                (k.GetCurrentThreadId, DW, ()),
                (k.OpenThread, H, (DW, BOOL, DW)),
                (k.GetProcessIdOfThread, DW, (H,)),
                (k.QueryFullProcessImageNameW, BOOL, (H, DW, wintypes.LPWSTR,
                                                      ctypes.POINTER(DW)))):
            f.restype, f.argtypes = r, a
        self.u32, self.k32 = u, k


def _api():
    if not _API:
        _API.append(_Api())
    return _API[0]


def class_name(hwnd):
    """The window's class, or None when it is gone."""
    buf = ctypes.create_unicode_buffer(256)
    n = _api().u32.GetClassNameW(hwnd, buf, 256)
    return buf.value if n else None


def window_text(hwnd):
    buf = ctypes.create_unicode_buffer(1024)
    _api().u32.GetWindowTextW(hwnd, buf, 1024)
    return buf.value


def window_pid(hwnd):
    pid = wintypes.DWORD()
    _api().u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


def thread_pid(tid):
    """The process of a thread id, or None."""
    if not tid:
        return None
    h = _api().k32.OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, tid)
    if not h:
        return None
    try:
        return _api().k32.GetProcessIdOfThread(h) or None
    finally:
        win32.close(h)


def exe_of(pid):
    """The full path of a process's image, or None."""
    if not pid:
        return None
    h = win32.K32.OpenProcess(win32.QUERY_LIMITED, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = wintypes.DWORD(32768)
        ok = _api().k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
        return buf.value if ok else None
    finally:
        win32.close(h)


def base(path):
    return os.path.basename(path or "").lower()


_EXT = r"\.(?:exe|com|bat|cmd)"
_LAUNCHED = re.compile(
    r'^(?:"(?P<q>(?:[A-Za-z]:\\|\\\\)[^"]*?' + _EXT + r')"'      # "<drive>\...\x.exe"
    r'|(?P<p>(?:[A-Za-z]:\\|\\\\)[^"<>|*?]*?' + _EXT + r')'      # <drive>\...\x.exe
    r'|"?(?P<b>[\w.()+-]+' + _EXT + r')"?)'                       # x.exe
    r'(?:\s.*)?$', re.I | re.S)
_ADMIN = "Administrator: "


def title_exe(title):
    """The program a launched-looking title names (its path or bare name), or None."""
    t = (title or "").strip()
    if t.startswith(_ADMIN):
        t = t[len(_ADMIN):]
    m = _LAUNCHED.match(t)
    return (m.group("q") or m.group("p") or m.group("b")) if m else None


def looks_launched(title):
    """True when a title looks like a launched program's path or command (the module docstring
    gives the rule and its limits)."""
    return title_exe(title) is not None


class Window:
    """One window the monitor saw open. how: 'window' (a console or Terminal window shown after
    start) or 'tab' (a pre-start Terminal window retitled to a launched program). pid and exe are
    the program's, when known; host_pid and host_exe the window's host (conhost, Windows Terminal).
    ours: True, False or None (unknown), as the monitor's ours() said for pid."""

    def __init__(self, cls, how, title, pid=None, exe=None, host_pid=None, host_exe=None,
                 ours=None, at=None, mono=None, hwnd=None):
        self.cls, self.how, self.title = cls, how, title
        self.pid, self.exe, self.host_pid, self.host_exe = pid, exe, host_pid, host_exe
        self.ours = ours
        self.at = time.time() if at is None else at
        self.mono = time.monotonic() if mono is None else mono
        self.title_mono = self.mono
        self.hwnd = hwnd

    def copy(self, **kw):
        w = Window(self.cls, self.how, self.title, self.pid, self.exe, self.host_pid,
                   self.host_exe, self.ours, self.at, self.mono, self.hwnd)
        w.title_mono = self.title_mono
        for k, v in kw.items():
            setattr(w, k, v)
        return w

    def tag(self):
        return {True: "this run", False: "not this run"}.get(self.ours, "run unknown")

    def text(self):
        when = time.strftime("%H:%M:%S", time.localtime(self.at))
        who = "pid %s %s" % (self.pid, base(self.exe) or "?") if self.pid else "pid ?"
        host = " via %s" % base(self.host_exe) if self.host_exe else ""
        return '%s %s %s%s %s title "%s" [%s]' % (when, self.how, who, host, self.cls,
                                                  self.title, self.tag())

    def as_dict(self):
        return {"at": round(self.at, 3), "cls": self.cls, "how": self.how, "title": self.title,
                "pid": self.pid, "exe": self.exe, "host_pid": self.host_pid,
                "host_exe": self.host_exe, "ours": self.ours}


class Pseudo:
    """A PseudoConsoleWindow shown: the program behind a Terminal window or tab."""

    def __init__(self, pid, exe, ours, mono=None):
        self.pid, self.exe, self.ours = pid, exe, ours
        self.mono = time.monotonic() if mono is None else mono


def _same_exe(exe, title):
    named = title_exe(title)
    return bool(exe and named) and (base(named) == base(exe))


def attribute(windows, pseudos, within=CORRELATE_S):
    """The windows to report: each Terminal window or tab paired with the program behind it (module
    docstring, "Correlation"). A console window keeps its own pid. A tab no program matches by
    exe, or one whose program is not known to be the caller's, is dropped as a likely tab
    switch. Pure: the inputs are not changed."""
    used, out = set(), []
    for w in sorted(windows, key=lambda w: w.mono):
        if w.cls != TERMINAL:
            out.append(w)
            continue
        near = [(i, p) for i, p in enumerate(pseudos) if i not in used and min(
            abs(p.mono - w.mono), abs(p.mono - w.title_mono)) <= within]
        dist = lambda ip: min(abs(ip[1].mono - w.mono), abs(ip[1].mono - w.title_mono))
        pick = sorted([ip for ip in near if _same_exe(ip[1].exe, w.title)], key=dist)
        if not pick:
            pick = sorted([ip for ip in near if ip[1].ours] if w.how == "tab" else near, key=dist)
        if not pick:
            if w.how == "window":
                out.append(w)
            continue
        i, p = pick[0]
        used.add(i)
        out.append(w.copy(pid=p.pid, exe=p.exe, ours=p.ours))
    return _drop_twins(out, within)


def _drop_twins(windows, within):
    """One window per twin pair: a window whose program is a console host (openconsole.exe,
    conhost.exe) beside a window of a known real program shown within `within` seconds is the
    host's twin of that program's window, so it is dropped and the program's window stays (with its
    own ours). The mate has the same ours; a host window already not counted (ours False) may take
    any mate, so a drop never lowers the count. Pairs are made nearest in time first, after a
    same-ours mate and then one whose host_pid is the host window's pid. Each real window drops at most one host window; a window with no exe
    is no mate; a host window with no partner stays."""
    real = [w for w in windows if w.exe and base(w.exe) not in HOSTS]
    hosts = [(j, w) for j, w in enumerate(windows) if base(w.exe) in HOSTS]
    pairs = sorted((r.ours != h.ours, r.host_pid != h.pid or not h.pid, abs(r.mono - h.mono), j, i)
                   for j, h in hosts for i, r in enumerate(real)
                   if abs(r.mono - h.mono) <= within and (r.ours == h.ours or h.ours is False))
    taken, drop = set(), set()
    for _, _, _, j, i in pairs:
        if j not in drop and i not in taken:
            taken.add(i)
            drop.add(j)
    return [w for j, w in enumerate(windows) if j not in drop]


class Monitor:
    """Records console windows that open between start() and stop(); a context manager.
    ours(pid) -> True, False or None says whose a program is (module docstring, "Whose window").
    error: why the monitor could not watch (None while it can). stop() returns the windows.
    classes: the window classes it records (WATCHED). A class other than TERMINAL is recorded as a
    console window is, with its own pid; a test passes a class of its own, so the window it opens
    to be seen counts in no other monitor (a gate's around the test run, say)."""

    def __init__(self, ours=None, classes=WATCHED):
        self._ours = ours
        self._classes = tuple(classes)
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread = None
        self._tid = None
        self._pre = set()
        self._new = {}              # hwnd -> Window, shown after start
        self._tabs = []             # Window, how='tab'
        self._tab_seen = set()      # (hwnd, title) of pre-start Terminal windows
        self._pseudo = []
        self.gone = 0
        self.faults = 0
        self.error = None
        self.windows = None         # set by stop()
        self._last = 0.0            # time.monotonic() of the last event delivered

    # -- the caller's side
    def start(self):
        if self._thread is not None:
            return self
        if os.name != "nt":
            self.error = "not Windows"
            return self
        self._thread = threading.Thread(target=self._run, name="kit-winwatch", daemon=True)
        self._thread.start()
        if not self._ready.wait(START_WAIT_S) and not self.error:
            self.error = "its thread did not start in %ds" % START_WAIT_S
        return self

    def stop(self):
        """Ends the thread (WM_QUIT; the thread removes its hooks) and returns the windows. Safe
        to call twice, or without start(). First it lets the events already raised arrive: it waits
        until none has come for QUIET_S, at most DRAIN_S (module docstring, "The tail")."""
        t = self._thread
        if t is not None and t.is_alive():
            if self._ready.is_set() and not self.error:
                self._drain()
            end = time.monotonic() + STOP_WAIT_S
            while t.is_alive() and time.monotonic() < end:
                if self._tid:
                    _api().u32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
                t.join(0.2)
            if t.is_alive() and not self.error:
                self.error = "its thread did not stop in %ds" % STOP_WAIT_S
        if self.windows is None:
            self.windows = self.snapshot()
        return self.windows

    def _drain(self):
        start = time.monotonic()
        end = start + DRAIN_S
        while True:
            now = time.monotonic()
            if now >= end or now - max(self._last, start) >= QUIET_S:
                return
            time.sleep(0.01)

    def snapshot(self):
        """The windows seen so far, attributed."""
        with self._lock:
            windows = list(self._new.values()) + list(self._tabs)
            pseudos = list(self._pseudo)
        return attribute(windows, pseudos)

    def pseudos(self):
        with self._lock:
            return list(self._pseudo)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    # -- the monitor thread
    def _run(self):
        api, hooks = _api(), []
        try:
            self._tid = api.k32.GetCurrentThreadId()
            msg = wintypes.MSG()
            api.u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)  # makes the queue
            self._seed()
            self._proc = WINEVENTPROC(self._on_event)
            for ev in (EVENT_OBJECT_SHOW, EVENT_OBJECT_NAMECHANGE):
                h = api.u32.SetWinEventHook(ev, ev, None, self._proc, 0, 0, WINEVENT_OUTOFCONTEXT)
                if not h:
                    raise OSError("SetWinEventHook failed, error %d" % ctypes.get_last_error())
                hooks.append(h)
        except Exception as e:
            self.error = "%s: %s" % (type(e).__name__, e)
            for h in hooks:
                api.u32.UnhookWinEvent(h)
            self._ready.set()
            return
        self._ready.set()
        try:
            while api.u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                api.u32.TranslateMessage(ctypes.byref(msg))
                api.u32.DispatchMessageW(ctypes.byref(msg))
        finally:
            for h in hooks:
                api.u32.UnhookWinEvent(h)

    def _seed(self):
        def each(hwnd, _):
            cls = class_name(hwnd)
            if cls in self._classes:
                self._pre.add(hwnd)
                if cls == TERMINAL:
                    self._tab_seen.add((hwnd, window_text(hwnd)))
            return True
        _api().u32.EnumWindows(WNDENUMPROC(each), 0)

    def _owner(self, pid):
        if self._ours is None or not pid:
            return None
        try:
            r = self._ours(pid)
        except Exception:
            return None
        return r if r in (True, False) else None

    def _on_event(self, hook, event, hwnd, id_object, id_child, thread, ms):
        self._last = time.monotonic()
        try:
            if id_object == OBJID_WINDOW and id_child == CHILDID_SELF and hwnd:
                self._event(event, hwnd, thread)
        except Exception:
            self.faults += 1

    def _event(self, event, hwnd, thread):
        cls = class_name(hwnd)
        if cls is None:
            if event == EVENT_OBJECT_SHOW and base(exe_of(thread_pid(thread))) in HOSTS:
                with self._lock:
                    self.gone += 1
            return
        if cls == PSEUDO:
            if event == EVENT_OBJECT_SHOW:
                pid = window_pid(hwnd)
                p = Pseudo(pid, exe_of(pid), self._owner(pid))
                with self._lock:
                    self._pseudo.append(p)
            return
        if cls not in self._classes or hwnd in self._pre:
            if (cls == TERMINAL and TERMINAL in self._classes
                    and event == EVENT_OBJECT_NAMECHANGE):
                self._retitled(hwnd)
            return
        if event == EVENT_OBJECT_SHOW and hwnd not in self._new:
            title, pid = window_text(hwnd), window_pid(hwnd)
            if cls != TERMINAL:
                host = thread_pid(thread)
                w = Window(cls, "window", title, pid, exe_of(pid), host, exe_of(host),
                           self._owner(pid), hwnd=hwnd)
            else:
                w = Window(cls, "window", title, None, None, pid, exe_of(pid), hwnd=hwnd)
            with self._lock:
                self._new[hwnd] = w
        elif event == EVENT_OBJECT_NAMECHANGE and hwnd in self._new:
            title = window_text(hwnd)
            with self._lock:
                w = self._new[hwnd]
                if title and not looks_launched(w.title):
                    w.title, w.title_mono = title, time.monotonic()

    def _retitled(self, hwnd):
        """A pre-start Terminal window's title changed: a candidate new tab."""
        title = window_text(hwnd)
        if not looks_launched(title) or (hwnd, title) in self._tab_seen:
            return
        self._tab_seen.add((hwnd, title))
        pid = window_pid(hwnd)
        w = Window(TERMINAL, "tab", title, None, None, pid, exe_of(pid), hwnd=hwnd)
        with self._lock:
            self._tabs.append(w)


def counted(windows):
    """The windows that count: those not known to be another run's."""
    return [w for w in windows or () if w.ours is not False]


def records(windows, cap=RECORD_CAP):
    """(the first cap windows as Window.as_dict(), how many more there were): what a record keeps."""
    windows = list(windows or ())
    return [w.as_dict() for w in windows[:cap]], max(0, len(windows) - cap)


def report_lines(windows, error=None, gone=0, fails=False):
    """The report: `windows opened: N` (N counts the windows not known to be another run's), one
    line per window, then the other runs' windows and the unread flashes, each apart. fails: the
    caller's verdict counts a window (kitlib.gate.runner.WINDOWS_FAIL); else a window only warns."""
    if error:
        return ["windows opened: unknown (the window monitor did not run: %s)" % error]
    ours = counted(windows)
    head = "windows opened: %d" % len(ours)
    if ours:
        head += (" - FAILED: a console window opened" if fails else
                 " - WARNING: a console window opened (the verdict does not count it)")
    lines = [head] + ["  " + w.text() for w in ours]
    others = [w for w in windows or () if w.ours is False]
    if others:
        lines.append("  also seen, another run's: %d" % len(others))
        lines += ["    " + w.text() for w in others]
    if gone:
        lines.append("  also seen: %d console host window(s) shown and gone before they could "
                     "be read" % gone)
    return lines
