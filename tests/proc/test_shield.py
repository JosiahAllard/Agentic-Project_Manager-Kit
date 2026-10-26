"""Tests for kitlib.proc.shield: a child's own process refuses PROCESS_TERMINATE by its pid while
shielded, and `unshield` gives it back; the pid hold keeps an exited process's pid
unused for its linger, and denies nothing. Windows only.

Every child here is the test's own, created suspended or not, and ended through its own Popen
handle, its job, or by running to its end; a grandchild is its parent's, which waits for it.
`can_terminate` opens a pid only to ask; no process is killed by its number.
"""
import ctypes
import os
import re
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests import testutil
from tests.platforms import windows_only

if os.name == 'nt':
    from kitlib.proc import job, shield, win32

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# A note the shield writes is printed by a caller that runs the child unshielded anyway; it must
# read as no failing line to a gate's fail patterns, so it never says "fail".
NO_RED = re.compile(r'(?i)fail')


def can_terminate(pid):
    """True if this process may open `pid` for PROCESS_TERMINATE; False on ERROR_ACCESS_DENIED."""
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = (ctypes.c_void_p,)
    h = k.OpenProcess(0x0001, False, pid)
    if h:
        k.CloseHandle(h)
        return True
    err = ctypes.get_last_error()
    if err != 5:
        raise AssertionError('OpenProcess(%d) returned error %d, not 5' % (pid, err))
    return False


@windows_only('the shield is Windows-only')
class TheShield(unittest.TestCase):

    def suspended(self):
        proc = subprocess.Popen([sys.executable, '-c', 'pass'], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=win32.CREATE_SUSPENDED)

        def end():
            proc.kill()             # our own handle: it holds every right, shield or not
            proc.wait()
        self.addCleanup(end)
        return proc

    def test_a_shielded_process_refuses_a_kill_by_pid_and_unshield_gives_it_back(self):
        proc = self.suspended()
        self.assertTrue(can_terminate(proc.pid))
        token = shield.shield(proc)
        self.assertFalse(can_terminate(proc.pid))
        shield.unshield(proc, token)
        self.assertTrue(can_terminate(proc.pid))

    def test_release_keeps_the_shield(self):
        proc = self.suspended()
        shield.release(shield.shield(proc))
        self.assertFalse(can_terminate(proc.pid))

    def test_the_owner_handle_still_ends_a_shielded_process(self):
        proc = self.suspended()
        shield.release(shield.shield(proc))
        self.assertEqual(win32.resume(proc), 0)
        proc.kill()
        self.assertEqual(proc.wait(timeout=30), 1)

    def test_a_null_dacl_is_left_alone(self):
        # A NULL DACL grants everyone everything; a deny entry merged into it would leave an ACL
        # holding only that entry. Nothing may be written: the pid stays killable.
        proc = self.suspended()
        null = mock.Mock(return_value=(ctypes.c_void_p(None), ctypes.c_void_p(None)))
        with self.assertRaisesRegex(OSError, 'NULL DACL'):
            shield.shield(proc, null)
        self.assertEqual(null.call_count, 1)
        self.assertTrue(can_terminate(proc.pid))

    def test_its_messages_are_no_red_line(self):
        for msg in ('GetSecurityInfo returned 5', 'SetEntriesInAclW returned 87',
                    'SetSecurityInfo returned 5', 'CreateWellKnownSid returned error 122',
                    'its process has a NULL DACL; left as it is'):
            self.assertIsNone(NO_RED.search(msg), msg)


# A child that opens its parent by pid with PROCESS_ALL_ACCESS - as .NET's Process does to watch
# a process exit (testhost opening vstest.console: a repository's unit tier under the kit gate,
# 2026-10-06) - and prints the error OpenProcess returned, 0 when it opened.
OPEN_PARENT = r'''
import ctypes, os
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.OpenProcess.restype = ctypes.c_void_p
k.CloseHandle.argtypes = (ctypes.c_void_p,)
h = k.OpenProcess(0x1FFFFF, False, os.getppid())
print(0 if h else ctypes.get_last_error(), flush=True)
if h:
    k.CloseHandle(h)
'''
# Runs `python -c <argv[1]> <argv[2:]...>` and exits with its exit code.
LAUNCH = ("import subprocess, sys; "
          "sys.exit(subprocess.call([sys.executable, '-c'] + sys.argv[1:]))")


# Prints its own pid, lives argv[1] seconds - long past the hold's 5 ms passes - and exits.
SHORT_LIFE = "import os, sys, time; print(os.getpid(), flush=True); time.sleep(float(sys.argv[1]))"


def listed_pids():
    """Every pid K32EnumProcesses lists now."""
    from ctypes import wintypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    size = 4096
    while True:
        buf, got = (wintypes.DWORD * size)(), wintypes.DWORD()
        if not k.K32EnumProcesses(buf, ctypes.sizeof(buf), ctypes.byref(got)):
            raise OSError('K32EnumProcesses returned error %d' % ctypes.get_last_error())
        if got.value // 4 < size:
            return set(buf[:got.value // 4])
        size *= 2


@windows_only('the pid hold is Windows-only')
class TheHold(unittest.TestCase):

    def hold(self, linger):
        held = shield.PidHold(linger=linger)
        self.addCleanup(held.close)
        held.warm(0)
        return held

    def test_an_exited_grandchilds_pid_stays_unused_for_the_linger_then_is_released(self):
        # The grandchild's only handles are its parent's Popen (gone when the parent exits) and
        # the hold's: within the linger its pid names an exited process, which Windows lists no
        # more and gives to no new process; after it the hold closes its handle.
        held = self.hold(2.0)
        since = held.seq()
        out = subprocess.run([sys.executable, '-c', LAUNCH, SHORT_LIFE, '0.3'],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        pid = int(out.stdout)
        self.assertTrue(held.holds(pid), held.record(since))   # red: no PidHold at all
        self.assertNotIn(pid, listed_pids())
        self.assertTrue(testutil.exited_object(pid))
        end = time.monotonic() + 10
        while (pid, 'released') not in [e[1:] for e in held.record(since)] and \
                time.monotonic() < end:
            time.sleep(0.05)
        events = [e[1:] for e in held.record(since) if e[1] == pid]
        self.assertEqual(events, [(pid, 'held'), (pid, 'exited'), (pid, 'released')])
        self.assertFalse(held.holds(pid))

    def test_record_since_holds_only_later_events(self):
        held = self.hold(0.5)
        since = held.seq()
        later = [e for e in held.record(since)]
        self.assertTrue(all(e[0] > since for e in later))
        self.assertEqual(held.record(held.seq()), [])

    def test_a_closed_hold_refuses_to_warm(self):
        held = self.hold(0.5)
        held.close()
        self.assertTrue(held.stopped)
        with self.assertRaisesRegex(OSError, 'closed'):
            held.warm(0)

    def test_a_failed_watcher_says_why_and_warm_raises(self):
        class Failing(shield.PidHold):
            def _listed(self):
                raise OSError('K32EnumProcesses returned error 8')
        held = Failing(linger=0.1)
        self.addCleanup(held.close)
        with self.assertRaisesRegex(OSError, 'K32EnumProcesses returned error 8'):
            held.warm(0, timeout=30)
        self.assertTrue(held.stopped)
        self.assertIsNone(NO_RED.search(str(held.error)), held.error)

    def test_warm_waits_out_the_seconds_after_the_first_pass(self):
        held = self.hold(0.5)
        start = time.monotonic()
        held.warm(0.4)                  # its first pass was at most a moment ago
        self.assertGreaterEqual(time.monotonic() - held._first_at, 0.4)
        self.assertLess(time.monotonic() - start, 5)

    def test_warm_waits_again_when_a_wait_wakes_early(self):
        # On Windows an Event.wait can return a fraction of a millisecond early (0.3997 s of
        # 0.4 s): warm waits until the clock reaches its target. Every
        # wait here wakes 50 ms early, so one wait alone would return well short of 0.4 s.
        held = self.hold(0.5)
        real, asked = held._done.wait, []

        def wakes_early(left):
            asked.append(left)
            return real(max(0.0, left - 0.05))
        with mock.patch.object(held._done, 'wait', wakes_early):
            held.warm(0.4)
        self.assertGreaterEqual(time.monotonic() - held._first_at, 0.4)
        self.assertGreater(len(asked), 1, asked)        # red: one early wait, then returned

    def test_a_tree_member_can_open_its_young_tree_parent_with_all_access(self):
        # .NET's Process opens a process with PROCESS_ALL_ACCESS to watch it exit (testhost opening
        # vstest.console: a repository's unit tier, 2026-10-06). Under a warmed hold, a job's
        # grandchild opens its parent - the job's young child - so: the hold may put no deny
        # entry on it. (The kit gate starts no hold, docs/gate.md, so this runs through
        # kitlib.proc.job.)
        if testutil.ancestor_reach():
            self.skipTest("an older kit's gate shields every new process of this one")
        self.hold(0.5)
        with job.Job() as j:
            p = j.start([sys.executable, '-c', LAUNCH, LAUNCH, OPEN_PARENT],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(out.strip(), '0', 'OpenProcess(PROCESS_ALL_ACCESS) of the parent '
                                           'returned error %s' % out.strip())


@windows_only('the shield is Windows-only')
class ShieldedBeforeResume(unittest.TestCase):
    """Job.start's before_resume runs while the child is suspended: a shield set there is on
    before the child runs one instruction (how kitlib.pytests starts each unit)."""

    def test_the_child_finds_itself_shielded_from_its_first_line(self):
        tokens = []
        with job.Job() as j:
            p = j.start([sys.executable, '-c', testutil.SHIELDED + 'print(shielded())'],
                        before_resume=lambda proc: tokens.append(shield.shield(proc)),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                out, err = p.communicate(timeout=60)
            finally:
                for t in tokens:
                    shield.release(t)
        self.assertEqual(len(tokens), 1)
        self.assertEqual(out.strip(), 'True', err)

    def test_an_unshielded_child_says_so(self):
        if testutil.ancestor_reach():
            self.skipTest("an older kit's gate shields every new process of this one")
        with job.Job() as j:
            p = j.start([sys.executable, '-c', testutil.SHIELDED + 'print(shielded())'],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out, err = p.communicate(timeout=60)
        self.assertEqual(out.strip(), 'False', err)


@windows_only('the pid hold is Windows-only')
class APublishedHoldIsFoundWhileItRuns(unittest.TestCase):
    """PidHold.publish / inherited: a process that publishes its warmed hold is found by a process
    it starts, only while that hold runs."""

    def setUp(self):
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop(shield.HOLD_ENV, None)
        self.held = shield.PidHold(linger=0.5)
        self.addCleanup(self.held.close)
        self.held.warm(0)

    def test_publish_names_its_beacon_in_the_environment_once(self):
        self.assertFalse(shield.inherited())
        name = self.held.publish()
        self.assertEqual(os.environ[shield.HOLD_ENV], name)
        self.assertTrue(shield.inherited())
        self.assertEqual(self.held.publish(), name)

    def test_a_child_process_finds_it(self):
        self.held.publish()
        code = ('import sys; sys.path.insert(0, %r); from kitlib.proc import shield; '
                'print(shield.inherited())' % ROOT)
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.stdout.strip(), 'True', out.stderr)

    def test_a_closed_hold_is_found_by_no_one_and_cannot_publish(self):
        self.held.publish()
        self.held.close()
        self.assertFalse(shield.inherited())               # the env still names it
        with self.assertRaisesRegex(OSError, 'closed'):
            self.held.publish()

    def test_a_failed_hold_is_found_by_no_one(self):
        import threading
        fail = threading.Event()

        class Failing(shield.PidHold):
            def _listed(self):
                if fail.is_set():
                    raise OSError('K32EnumProcesses returned error 8')
                return set()
        held = Failing(linger=0.1)
        self.addCleanup(held.close)
        held.warm(0)
        held.publish()
        self.assertTrue(shield.inherited())
        fail.set()
        held._done.wait(30)
        self.assertTrue(held.stopped)
        self.assertFalse(shield.inherited())

    def test_no_name_or_an_unknown_one_is_not_inherited(self):
        self.assertFalse(shield.inherited())
        os.environ[shield.HOLD_ENV] = 'Local\\kit-pid-hold-0-0'
        self.assertFalse(shield.inherited())


@windows_only('the pid hold is Windows-only')
class TheHoldIsMadeOnceAndClosedAtExit(unittest.TestCase):
    """shield.hold: two first calls at once make one PidHold (not two, one leaked), and the
    one it makes is closed at interpreter exit."""

    def setUp(self):
        self.made = []
        made = self.made

        class Slow(object):
            def __init__(self):
                time.sleep(0.2)                 # wide enough for the second call to arrive
                made.append(self)

            def close(self):
                pass
        saved = shield._HOLD
        shield._HOLD = None

        def restore():
            shield._HOLD = saved
        self.addCleanup(restore)
        self.registered = []
        for patch in (mock.patch.object(shield, 'PidHold', Slow),
                      mock.patch.object(shield.atexit, 'register', self.registered.append)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_two_first_calls_at_once_make_one_hold(self):
        import threading
        got = []
        threads = [threading.Thread(target=lambda: got.append(shield.hold()))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len(self.made), 1)                    # red: two, one never closed
        self.assertEqual(got, [self.made[0]] * 2)

    def test_the_hold_is_closed_at_exit(self):
        held = shield.hold()
        self.assertEqual(self.registered, [held.close])        # red: never registered


if __name__ == '__main__':
    unittest.main()
