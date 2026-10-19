"""Tests for kitlib.proc.winwatch: the window monitor. The real-process tests start their own
children only, end them by their Popen handle, and wait for events with a deadline.

No test here shows a window the user can see. RealWindows' child shows a 1x1 tool window off
screen, never activated, of a class of its own (PROBE_CLASS) that only the test's monitor records.
The one test that opens a real console window (CREATE_NEW_CONSOLE) runs only when the environment
sets KIT_WINDOW_TESTS=1 (docs/test-tools.md "Window tests")."""
import ctypes
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

if os.name == "nt":
    from ctypes import wintypes

    from kitlib.proc import win32, winwatch

EVENT_WAIT_S = 15       # a window's events reach the monitor well within this
EXIT_WAIT_S = 30
PROBE_WAIT_S = 30       # the probe child prints its window's handle well within this
WINDOW_TESTS_ENV = "KIT_WINDOW_TESTS"
PROBE_CLASS = "KitWinwatchProbe"
PROBE_TITLE = "kit winwatch probe"

# The probe child: argv[1] the window class, argv[2] its title. It shows a 1x1 popup tool window
# off screen without activating it (no taskbar button, no focus), prints the window's handle, pumps
# its messages until its stdin closes (or 60 s pass), then destroys the window and exits 0. The
# test starts it with CREATE_NO_WINDOW, so it has no console either.
PROBE_CHILD = r'''
import ctypes, sys, threading, time
from ctypes import wintypes as W
u, k = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32")
LRESULT = ctypes.c_ssize_t
PROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", W.UINT), ("proc", PROC), ("cls_extra", ctypes.c_int),
                ("wnd_extra", ctypes.c_int), ("inst", W.HINSTANCE), ("icon", W.HICON),
                ("cursor", W.HANDLE), ("brush", W.HBRUSH), ("menu", W.LPCWSTR),
                ("name", W.LPCWSTR)]
u.DefWindowProcW.restype = LRESULT
u.DefWindowProcW.argtypes = (W.HWND, W.UINT, W.WPARAM, W.LPARAM)
u.RegisterClassW.restype, u.RegisterClassW.argtypes = W.ATOM, (ctypes.POINTER(WNDCLASSW),)
u.CreateWindowExW.restype = W.HWND
u.CreateWindowExW.argtypes = (W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)
u.ShowWindow.argtypes, u.DestroyWindow.argtypes = (W.HWND, ctypes.c_int), (W.HWND,)
u.PeekMessageW.argtypes = (ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT)
u.DispatchMessageW.argtypes = (ctypes.POINTER(W.MSG),)
k.GetModuleHandleW.restype, k.GetModuleHandleW.argtypes = W.HMODULE, (W.LPCWSTR,)
proc = PROC(lambda h, m, wp, lp: u.DefWindowProcW(h, m, wp, lp))
inst = k.GetModuleHandleW(None)
wc = WNDCLASSW(proc=proc, inst=inst, name=sys.argv[1])
if not u.RegisterClassW(ctypes.byref(wc)):
    sys.exit("RegisterClassW failed, error %d" % ctypes.get_last_error())
WS_EX = 0x00000080 | 0x08000000          # WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
hwnd = u.CreateWindowExW(WS_EX, sys.argv[1], sys.argv[2], 0x80000000,  # WS_POPUP
                         -32000, -32000, 1, 1, None, None, inst, None)
if not hwnd:
    sys.exit("CreateWindowExW failed, error %d" % ctypes.get_last_error())
u.ShowWindow(hwnd, 4)                    # SW_SHOWNOACTIVATE
print(hwnd, flush=True)
done = threading.Event()
threading.Thread(target=lambda: (sys.stdin.read(), done.set()), daemon=True).start()
msg, end = W.MSG(), time.monotonic() + 60
while not done.is_set() and time.monotonic() < end:
    while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):      # PM_REMOVE
        u.DispatchMessageW(ctypes.byref(msg))
    time.sleep(0.02)
u.DestroyWindow(hwnd)
'''


def python_exe():
    """The console python beside this interpreter (sys.executable may be pythonw)."""
    d = os.path.dirname(sys.executable)
    p = os.path.join(d, "python.exe")
    return p if os.path.exists(p) else sys.executable


def watchers():
    return [t for t in threading.enumerate() if t.name == "kit-winwatch"]


def on_a_monitor(hwnd):
    """The display monitor the window is on (MonitorFromWindow, MONITOR_DEFAULTTONULL), 0 when it
    is on none."""
    u = ctypes.WinDLL("user32")
    u.MonitorFromWindow.restype = wintypes.HANDLE
    u.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
    return u.MonitorFromWindow(hwnd, 0) or 0


def no_desktop():
    """Why this process can see no window event, or None when it runs in an interactive session:
    session 0 (a service), a window station with no display, or a session no user is connected
    to (a disconnected remote session)."""
    k, u = ctypes.WinDLL("kernel32"), ctypes.WinDLL("user32")
    sid = wintypes.DWORD()
    k.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    if k.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)) and sid.value == 0:
        return "session 0"

    class Flags(ctypes.Structure):          # USEROBJECTFLAGS
        _fields_ = [("inherit", wintypes.BOOL), ("reserved", wintypes.BOOL),
                    ("flags", wintypes.DWORD)]
    u.GetProcessWindowStation.restype = wintypes.HANDLE
    u.GetUserObjectInformationW.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    f, n = Flags(), wintypes.DWORD()
    if u.GetUserObjectInformationW(u.GetProcessWindowStation(), 1, ctypes.byref(f),  # UOI_FLAGS
                                   ctypes.sizeof(f), ctypes.byref(n)) and not f.flags & 1:
        return "a window station with no display"                               # WSF_VISIBLE
    w = ctypes.WinDLL("wtsapi32")
    w.WTSQuerySessionInformationW.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                                              ctypes.POINTER(ctypes.c_void_p),
                                              ctypes.POINTER(wintypes.DWORD))
    w.WTSFreeMemory.argtypes = (ctypes.c_void_p,)
    buf, size = ctypes.c_void_p(), wintypes.DWORD()
    if w.WTSQuerySessionInformationW(None, 0xFFFFFFFF, 8, ctypes.byref(buf),  # WTSConnectState
                                     ctypes.byref(size)):
        try:
            state = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int))[0]
        finally:
            w.WTSFreeMemory(buf)
        if state != 0:                                                      # WTSActive
            return "a session no user is connected to (WTS state %d)" % state
    return None


class Watched(unittest.TestCase):
    """A monitor around children of this test (self.mine: their pids). classes: the monitor's."""
    classes = None

    def setUp(self):
        self.mine = set()
        kw = {"classes": self.classes} if self.classes else {}
        self.mon = winwatch.Monitor(ours=lambda pid: pid in self.mine, **kw)
        self.addCleanup(self.mon.stop)
        self.mon.start()
        if self.mon.error:
            self.skipTest("the window monitor cannot run here: %s" % self.mon.error)

    def child(self, argv, flags, **kw):
        p = subprocess.Popen([python_exe()] + argv, creationflags=flags, **kw)
        self.mine.add(p.pid)
        self.addCleanup(self.end, p)
        return p

    def probe(self):
        """The probe child (PROBE_CHILD), once its window shows; returns (Popen, hwnd). A child
        that prints no handle within PROBE_WAIT_S is killed and the test fails: the line is read
        on a thread, so a stuck child cannot hang the test."""
        p = self.child(["-c", PROBE_CHILD, PROBE_CLASS, PROBE_TITLE], win32.CREATE_NO_WINDOW,
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        got = []
        reader = threading.Thread(target=lambda: got.append(p.stdout.readline()), daemon=True)
        reader.start()
        reader.join(PROBE_WAIT_S)
        line = got[0].strip() if got else b""
        if not line:
            self.end(p)                 # the kill gives the reader its end of file
            reader.join(EXIT_WAIT_S)
            self.fail("the probe child showed no window in %ds (exit %s)" % (PROBE_WAIT_S,
                                                                            p.returncode))
        return p, int(line)

    def end(self, p):
        if p.poll() is None:
            p.kill()
            p.wait(EXIT_WAIT_S)
        for f in (p.stdin, p.stdout):
            if f is not None:
                f.close()

    def seen(self, pid):
        """The test monitor's windows of pid, once one shows (EVENT_WAIT_S at most). When none
        shows, the test skips if this process has no desktop to see one on (no_desktop); in an
        interactive session it returns [] and the test fails."""
        end, seen = time.monotonic() + EVENT_WAIT_S, []
        while time.monotonic() < end and not seen:
            seen = [w for w in self.mon.snapshot() if w.pid == pid]
            time.sleep(0.02)
        if not seen:
            why = no_desktop()
            if why:
                self.skipTest("no window event came in %ds: %s" % (EVENT_WAIT_S, why))
        return seen


@unittest.skipUnless(os.name == "nt", "Windows only")
class RealWindows(Watched):
    """The probe child's off-screen window, of PROBE_CLASS: no window the user can see, and one no
    other monitor records (a gate's, around this test run, records winwatch.WATCHED only)."""
    classes = (PROBE_CLASS,)

    def test_a_window_is_recorded_with_its_pid_and_owner(self):
        p, hwnd = self.probe()
        seen = self.seen(p.pid)
        self.assertEqual(on_a_monitor(hwnd), 0)     # shown, yet on no display: no one sees it
        p.stdin.close()
        self.assertEqual(p.wait(EXIT_WAIT_S), 0)
        windows = self.mon.stop()
        self.assertTrue(seen, [w.text() for w in windows])
        w = [w for w in windows if w.pid == p.pid][0]
        self.assertEqual((w.cls, w.how, w.hwnd, w.title),
                         (PROBE_CLASS, "window", hwnd, PROBE_TITLE))
        self.assertEqual(winwatch.base(w.exe), "python.exe")
        self.assertEqual(w.host_pid, p.pid)         # the window's thread is the child's own
        self.assertIs(w.ours, True)
        lines = winwatch.report_lines([w])
        self.assertTrue(lines[0].startswith("windows opened: 1 - WARNING"), lines)
        self.assertIn("pid %d python.exe" % p.pid, lines[1])
        self.assertTrue(lines[1].endswith("[this run]"), lines)

    def test_a_window_shown_just_before_stop_is_recorded(self):
        """The tail: stop() straight after the probe's window shows (it prints its handle after
        ShowWindow), with no wait for the event, still records it. It guards that contract, not
        the drain itself: the event is queued before the probe prints and WM_QUIT is read after
        it, so here it passes with the drain off too (a later-queued event is the drain's case)."""
        p, hwnd = self.probe()
        windows = self.mon.stop()
        mine = [w for w in windows if w.pid == p.pid]
        if not mine:
            why = no_desktop()
            if why:
                self.skipTest("no window event came: %s" % why)
        self.assertEqual([(w.cls, w.hwnd) for w in mine], [(PROBE_CLASS, hwnd)])

    def test_a_monitor_records_only_its_classes(self):
        """A monitor of the default classes (WATCHED), as a gate's is, does not record the probe."""
        mon = winwatch.Monitor(ours=lambda pid: True).start()
        self.addCleanup(mon.stop)
        p, _ = self.probe()
        self.assertTrue(self.seen(p.pid))           # the test's monitor saw it, so mon could
        p.stdin.close()
        self.assertEqual(p.wait(EXIT_WAIT_S), 0)
        self.assertEqual([w.text() for w in mon.stop() if w.pid == p.pid], [])

    def test_a_no_window_child_is_not_recorded(self):
        p = self.child(["-c", "import time; time.sleep(0.5)"], win32.CREATE_NO_WINDOW,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        self.assertEqual(p.wait(EXIT_WAIT_S), 0)
        time.sleep(1.0)                  # any late event of it has arrived
        windows = self.mon.stop()
        self.assertEqual([w.text() for w in windows if w.pid == p.pid], [])
        self.assertEqual([x.pid for x in self.mon.pseudos() if x.pid == p.pid], [])


@unittest.skipUnless(os.name == "nt", "Windows only")
@unittest.skipUnless(os.environ.get(WINDOW_TESTS_ENV) == "1",
                     "opens a console window the user sees; %s=1 runs it" % WINDOW_TESTS_ENV)
class ConsoleWindow(Watched):
    """Opt-in (KIT_WINDOW_TESTS=1): a real console window, of the classes a gate watches for."""

    def test_a_new_console_child_is_recorded_with_its_pid(self):
        """CREATE_NEW_CONSOLE opens a console window (or a Windows Terminal window or tab) the
        user sees, for as long as it takes the monitor to see it. The child waits on its stdin and
        ends with exit 0 when the test closes it (a Terminal window stays open after a non-zero
        exit)."""
        p = self.child(["-c", "import sys; sys.stdin.read()"], win32.CREATE_NEW_CONSOLE,
                       stdin=subprocess.PIPE)
        seen = self.seen(p.pid)
        p.stdin.close()
        self.assertEqual(p.wait(EXIT_WAIT_S), 0)
        windows = self.mon.stop()
        self.assertTrue(seen, [w.text() for w in windows])
        w = [w for w in windows if w.pid == p.pid][0]
        self.assertIn(w.cls, (winwatch.CONSOLE, winwatch.TERMINAL))
        self.assertEqual(winwatch.base(w.exe), "python.exe")
        self.assertIs(w.ours, True)
        self.assertEqual(winwatch.counted(windows), [x for x in windows if x.ours is not False])
        self.assertTrue(winwatch.report_lines(windows)[0].startswith("windows opened: "))


@unittest.skipUnless(os.name == "nt", "Windows only")
class StartStop(unittest.TestCase):
    def test_start_and_stop_leave_no_thread(self):
        before = len(watchers())
        mon = winwatch.Monitor()
        with mon:
            self.assertIsNone(mon.error)
            self.assertEqual(len(watchers()), before + 1)
        self.assertEqual(len(watchers()), before)
        self.assertIsNotNone(mon.windows)
        self.assertIs(mon.stop(), mon.windows)         # a second stop: the same list, no wait

    def test_stop_without_start(self):
        mon = winwatch.Monitor()
        self.assertEqual(mon.stop(), [])
        self.assertIsNone(mon.error)

    def test_stop_waits_for_quiet_and_no_longer_than_drain(self):
        mon = winwatch.Monitor().start()
        self.addCleanup(mon.stop)
        t0 = time.monotonic()
        with mock.patch.object(winwatch, "QUIET_S", 3600):     # never quiet long enough
            mon.stop()
        took = time.monotonic() - t0
        self.assertGreaterEqual(took, winwatch.DRAIN_S - 0.02)
        self.assertLess(took, winwatch.DRAIN_S + 2.0)   # bounded (plus the thread's own end)
        quiet = winwatch.Monitor().start()
        self.addCleanup(quiet.stop)
        t0 = time.monotonic()
        quiet.stop()
        self.assertGreaterEqual(time.monotonic() - t0, winwatch.QUIET_S - 0.02)

    def test_a_raising_ours_is_unknown(self):
        def boom(pid):
            raise RuntimeError("x")
        mon = winwatch.Monitor(ours=boom)
        self.assertIsNone(mon._owner(1234))
        self.assertIsNone(winwatch.Monitor()._owner(1234))
        self.assertIs(winwatch.Monitor(ours=lambda pid: True)._owner(1234), True)
        self.assertIsNone(winwatch.Monitor(ours=lambda pid: "in")._owner(1234))


class LooksLaunched(unittest.TestCase):
    def test_paths_and_names(self):
        for t in (r"C:\Python\python.exe", r'"C:\Program Files\Git\bin\bash.exe" --login',
                  r"C:\Windows\system32\cmd.exe - python x.py", "Administrator: cmd.exe",
                  r"\\server\share\tool.CMD", "python.exe", r"D:\a b\run.bat arg"):
            with self.subTest(t):
                self.assertTrue(winwatch.looks_launched(t))

    def test_not_launched(self):
        for t in ("Windows Terminal", "Terminal", "Windows PowerShell", "", None,
                  "python x.py", r"C:\work\notes.txt", "build: 3 of 9 .exe files"):
            with self.subTest(t):
                self.assertFalse(winwatch.looks_launched(t))

    def test_title_exe(self):
        self.assertEqual(winwatch.title_exe(r'"C:\P F\x.exe" a'), r"C:\P F\x.exe")
        self.assertEqual(winwatch.title_exe(r"C:\W\cmd.exe - git status"), r"C:\W\cmd.exe")
        self.assertEqual(winwatch.title_exe("Administrator: x.com"), "x.com")


def W(cls, how, title, mono, **kw):
    return winwatch.Window(cls, how, title, at=0, mono=mono, **kw)


class Attribute(unittest.TestCase):
    PY = r"C:\Py\python.exe"
    GIT = r"C:\Git\git.exe"

    def test_terminal_window_takes_its_program(self):
        w = W(winwatch.TERMINAL, "window", self.PY, 10.0, host_pid=7)
        out = winwatch.attribute([w], [winwatch.Pseudo(55, self.PY, True, mono=10.2)])
        self.assertEqual((out[0].pid, out[0].exe, out[0].ours), (55, self.PY, True))
        self.assertIsNone(w.pid)                    # the input is not changed

    def test_the_exe_the_title_names_wins_over_time(self):
        w = W(winwatch.TERMINAL, "window", self.GIT, 10.0)
        out = winwatch.attribute([w], [winwatch.Pseudo(1, self.PY, True, mono=10.0),
                                       winwatch.Pseudo(2, self.GIT, False, mono=12.0)])
        self.assertEqual(out[0].pid, 2)

    def test_each_program_is_used_once(self):
        a = W(winwatch.TERMINAL, "window", self.PY, 10.0)
        b = W(winwatch.TERMINAL, "window", self.PY, 11.0)
        out = winwatch.attribute([a, b], [winwatch.Pseudo(1, self.PY, True, mono=10.1)])
        self.assertEqual([w.pid for w in out], [1, None])     # b still counts, unattributed

    def test_too_far_apart_is_not_paired(self):
        w = W(winwatch.TERMINAL, "window", self.PY, 10.0)
        out = winwatch.attribute([w], [winwatch.Pseudo(1, self.PY, True,
                                                       mono=10.0 + winwatch.CORRELATE_S + 1)])
        self.assertIsNone(out[0].pid)

    def test_a_tab_needs_a_program(self):
        t = W(winwatch.TERMINAL, "tab", self.PY, 10.0)
        self.assertEqual(winwatch.attribute([t], []), [])                    # a tab switch
        self.assertEqual(winwatch.attribute([t], [winwatch.Pseudo(1, self.GIT, None,
                                                                  mono=10.0)]), [])
        out = winwatch.attribute([t], [winwatch.Pseudo(1, self.GIT, True, mono=10.0)])
        self.assertEqual(out[0].pid, 1)                                      # the run's own
        out = winwatch.attribute([t], [winwatch.Pseudo(3, self.PY, False, mono=10.0)])
        self.assertEqual((out[0].pid, out[0].ours), (3, False))              # by exe

    def test_a_host_twin_is_reported_once_as_the_program(self):
        oc = r"C:\Term\OpenConsole.exe"
        real = W(winwatch.CONSOLE, "window", "a", 10.0, pid=5, exe=self.PY, ours=True)
        twin = W(winwatch.CONSOLE, "window", "a", 10.1, pid=6, exe=oc, ours=False)
        far = W(winwatch.CONSOLE, "window", "b", 30.0, pid=7, exe=oc, ours=False)
        out = winwatch.attribute([twin, real, far], [])
        self.assertEqual([w.pid for w in out], [5, 7])      # the lone host window stays
        lines = winwatch.report_lines(out)
        self.assertTrue(lines[0].startswith("windows opened: 1 "), lines)
        self.assertEqual(len([x for x in lines if "[not this run]" in x]), 1, lines)

    def test_a_console_window_keeps_its_pid(self):
        w = W(winwatch.CONSOLE, "window", "x", 10.0, pid=9, ours=True)
        out = winwatch.attribute([w], [winwatch.Pseudo(1, self.PY, True, mono=10.0)])
        self.assertEqual(out[0].pid, 9)


class Report(unittest.TestCase):
    def test_none(self):
        self.assertEqual(winwatch.report_lines([]), ["windows opened: 0"])

    def test_counted_and_others(self):
        ours = W(winwatch.CONSOLE, "window", "t", 1.0, pid=5, exe=r"C:\x\py.exe", ours=True)
        unknown = W(winwatch.CONSOLE, "window", "u", 2.0, pid=6, ours=None)
        other = W(winwatch.TERMINAL, "tab", "o", 3.0, pid=7, ours=False)
        lines = winwatch.report_lines([ours, unknown, other], gone=2)
        self.assertTrue(lines[0].startswith("windows opened: 2 - WARNING"), lines)
        self.assertIn("pid 5 py.exe", lines[1])
        self.assertIn("[this run]", lines[1])
        self.assertIn("[run unknown]", lines[2])
        self.assertEqual(lines[3], "  also seen, another run's: 1")
        self.assertIn("[not this run]", lines[4])
        self.assertIn("2 console host window(s)", lines[5])

    def test_fails_wording(self):
        ours = W(winwatch.CONSOLE, "window", "t", 1.0, pid=5, ours=True)
        self.assertEqual(winwatch.report_lines([ours], fails=True)[0],
                         "windows opened: 1 - FAILED: a console window opened")
        self.assertEqual(winwatch.report_lines([], fails=True), ["windows opened: 0"])

    def test_error(self):
        self.assertEqual(winwatch.report_lines([], error="boom"),
                         ["windows opened: unknown (the window monitor did not run: boom)"])

    def test_as_dict(self):
        d = W(winwatch.CONSOLE, "window", "t", 1.0, pid=5, ours=True).as_dict()
        self.assertEqual((d["cls"], d["how"], d["pid"], d["ours"]),
                         (winwatch.CONSOLE, "window", 5, True))

    def test_records_are_capped(self):
        ws = [W(winwatch.CONSOLE, "window", "t", 1.0, pid=i, ours=True) for i in range(5)]
        kept, cut = winwatch.records(ws, cap=3)
        self.assertEqual(([d["pid"] for d in kept], cut), ([0, 1, 2], 2))
        self.assertEqual(winwatch.records(ws), ([w.as_dict() for w in ws], 0))
        self.assertEqual(winwatch.records(None), ([], 0))


if __name__ == "__main__":
    unittest.main()
