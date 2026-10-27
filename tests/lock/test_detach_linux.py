"""kitlib.platform.linux.detach, kitlib.lock.detach's Linux backend: the argv it builds, the
scope or the bare setsid start, the trampoline's answer read back, and its failures; and
kitlib.lock.detach sending each call there on Linux. The trampoline itself runs here for real
(it needs /proc only to find a unit, which these runs name none of or never reach). Live: the
detached run outliving its caller's whole scope, through the user's systemd (skipped without
one; docs/linux-support-plan.md slice 7)."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import kitlib.platform
from kitlib.platform.linux import contain
from kitlib.platform.linux import detach as ld
from tests.platforms import posix_only, windows_only

if os.name == "nt":
    from kitlib.lock import detach as dt


class FakePopen:
    """subprocess.Popen's stand-in: records the call; communicate answers `out`."""
    calls = []

    def __init__(self, out="ok 4242\n", err="", code=0, hang=False, fail=None):
        self.out, self.err, self.code, self.hang, self.fail = out, err, code, hang, fail

    def __call__(self, argv, **kw):
        if self.fail:
            raise self.fail
        self.argv, self.kw, self.returncode, self.killed = argv, kw, None, False
        return self

    def communicate(self, data=None, timeout=None):
        if data is not None:
            self.data = json.loads(data)
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("python", timeout)
        self.returncode = self.code
        return self.out, self.err

    def kill(self):
        self.killed = True


def no_scope(case):
    p = mock.patch.object(ld, "scope", return_value=None)
    p.start()
    case.addCleanup(p.stop)


class Argv(unittest.TestCase):
    def test_command_is_python_dash_b_the_tool_then_the_id_and_the_command(self):
        argv = ld.command("py", "tools/machine_lock.py", ["run", "--slug", "s"], "r-1",
                          ["--", "echo", "hi"])
        self.assertEqual(argv, [os.path.abspath("py"), "-B",
                                os.path.abspath("tools/machine_lock.py"), "run", "--slug", "s",
                                "--detached-id", "r-1", "--", "echo", "hi"])

    def test_tool_command_has_no_id(self):
        self.assertEqual(ld.tool_command("py", "t.py", ["a"]),
                         [os.path.abspath("py"), "-B", os.path.abspath("t.py"), "a"])

    def test_missing_names_the_python_then_the_tool(self):
        d = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, d)
        tool = os.path.abspath(__file__)
        self.assertIsNone(ld.missing(ld.tool_command(sys.executable, tool, [])))
        self.assertIn("python is not a file",
                      ld.missing(ld.tool_command(os.path.join(d, "py"), tool, [])))
        self.assertIn("the tool is not a file",
                      ld.missing(ld.tool_command(sys.executable, os.path.join(d, "t.py"), [])))


class Scope(unittest.TestCase):
    def patched(self, tool="/usr/bin/systemd-run", bus=True, cg=True):
        for name, value in (("_tool", lambda n: tool), ("cgroup2", lambda: cg),
                            ("bus_env", lambda env: dict(env, XDG_RUNTIME_DIR="/run/user/1")
                             if bus else None),
                            ("no_expand", lambda run: ["--expand-environment=no"]),
                            ("prefix", lambda: "kt"), ("_tag", lambda: "1-2-abc")):
            p = mock.patch.object(contain, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_a_reachable_user_systemd_gives_a_detach_scope_of_its_own(self):
        self.patched()
        head, unit, env = ld.scope({"A": "1"})
        self.assertEqual(unit, "kt-detach-1-2-abc")
        self.assertEqual(head, ["/usr/bin/systemd-run", "--user", "--scope", "--quiet",
                                "--collect", "--expand-environment=no", "--unit",
                                "kt-detach-1-2-abc", "--"])
        self.assertEqual(env, {"A": "1", "XDG_RUNTIME_DIR": "/run/user/1"})
        self.assertNotIn("-job-", unit, "a kit job's scope name (contain) is never made here")

    def test_no_systemd_run_no_bus_or_no_cgroup2_is_no_scope(self):
        for kw in ({"tool": None}, {"bus": False}, {"cg": False}):
            with self.subTest(**kw):
                self.patched(**kw)
                self.assertIsNone(ld.scope({}))

    def test_create_in_a_scope_starts_systemd_run_and_waits_for_its_unit(self):
        self.patched()
        fake = FakePopen()
        self.assertEqual(ld.create(["py", "-B", "t"], "/w", {"A": "1"}, popen=fake), (4242, None))
        self.assertEqual(fake.data["argv"][:2], ["/usr/bin/systemd-run", "--user"])
        self.assertEqual(fake.data["argv"][-4:], ["--", "py", "-B", "t"])
        self.assertEqual(fake.data["unit"], "kt-detach-1-2-abc")
        self.assertEqual(fake.kw["env"], {"A": "1", "XDG_RUNTIME_DIR": "/run/user/1"})


class Create(unittest.TestCase):
    def setUp(self):
        no_scope(self)

    def test_bare_it_starts_the_trampoline_in_a_session_of_its_own(self):
        fake = FakePopen()
        self.assertEqual(ld.create(["py", "-B", "t"], "/w", {"A": "1"}, popen=fake), (4242, None))
        self.assertEqual(fake.argv, [sys.executable, "-I", "-c", ld.TRAMPOLINE])
        self.assertTrue(fake.kw["start_new_session"])
        self.assertEqual((fake.kw["cwd"], fake.kw["env"]), ("/w", {"A": "1"}))
        self.assertEqual(fake.data, {"argv": ["py", "-B", "t"], "unit": None,
                                     "wait_s": ld.READY_S})

    def test_the_trampoline_s_reason_is_the_answer(self):
        r = ld.create(["py"], "/w", {}, popen=FakePopen(out="x could not start: no such file\n"))
        self.assertEqual(r, (None, "could not start: no such file"))

    def test_an_answer_that_is_no_pid_is_quoted(self):
        pid, why = ld.create(["py"], "/w", {}, popen=FakePopen(out="hello", err="boom", code=1))
        self.assertIsNone(pid)
        self.assertEqual(why, "got 'hello' (exit 1): boom")

    def test_a_trampoline_that_does_not_answer_is_killed(self):
        fake = FakePopen(hang=True)
        pid, why = ld.create(["py"], "/w", {}, popen=fake, timeout=1)
        self.assertIsNone(pid)
        self.assertTrue(fake.killed)
        self.assertIn("did not answer within 1 s", why)

    def test_a_trampoline_that_cannot_start_says_so(self):
        pid, why = ld.create(["py"], "/w", {}, popen=FakePopen(fail=OSError("no python")))
        self.assertEqual((pid, why), (None, "failed: no python"))


def alive(pid):
    if os.name == "nt":
        from kitlib.proc import process
        return process.proc_ctime(pid) is not None
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, 9)


class Trampoline(unittest.TestCase):
    """The trampoline script for real, in this interpreter."""

    def answer(self, argv, unit=None, wait_s=0.5):
        r = subprocess.run([sys.executable, "-I", "-c", ld.TRAMPOLINE], capture_output=True,
                           text=True, timeout=60,
                           input=json.dumps({"argv": argv, "unit": unit, "wait_s": wait_s}))
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return r.stdout.strip()

    def test_with_no_unit_it_says_the_pid_at_once_and_leaves_the_run_running(self):
        out = self.answer([sys.executable, "-c", "import time; time.sleep(20)"])
        kind, pid = out.split()
        self.assertEqual(kind, "ok")
        self.addCleanup(kill, int(pid))
        self.assertTrue(alive(int(pid)), "the run outlives the trampoline")

    def test_a_run_that_cannot_start_is_said(self):
        self.assertTrue(self.answer([os.path.join(tempfile.gettempdir(), "no-such-prog-x")])
                        .startswith("x could not start: "))

    def test_a_wrapper_that_exits_before_its_unit_is_said(self):
        out = self.answer([sys.executable, "-c", "pass"], unit="kt-detach-1", wait_s=30)
        self.assertTrue(out.startswith("x systemd-run exited 0 before the run was in "
                                       "kt-detach-1.scope"), out)

    def test_a_run_never_in_its_unit_is_ended(self):
        out = self.answer([sys.executable, "-c", "import time; time.sleep(20)"],
                          unit="kt-detach-2", wait_s=0.3)
        self.assertTrue(out.startswith("x the run was not in kt-detach-2.scope within"), out)

    def test_create_end_to_end_with_no_scope(self):
        no_scope(self)
        pid, why = ld.create([sys.executable, "-c", "import time; time.sleep(20)"],
                             tempfile.gettempdir(), dict(os.environ))
        self.assertIsNone(why)
        self.addCleanup(kill, pid)
        self.assertTrue(alive(pid))
        self.assertEqual(ld.wrapper_pid(pid), pid)


LIVE_PREFIX = "pmkit-detachtest-%d" % os.getpid()
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The caller: starts the detached run (it writes its own pid to argv[2]), says both pids, waits.
CALLER = """import os, sys, time
sys.path.insert(0, sys.argv[1])
from kitlib.platform.linux import detach as ld
run = [sys.executable, '-c', 'import os, sys, time; open(sys.argv[1], "w").write(str(os.getpid())); '
       'time.sleep(120)', sys.argv[2]]
pid, why = ld.create(run, sys.argv[1], dict(os.environ))
print(os.getpid(), os.getsid(0), pid, why, flush=True)
time.sleep(120)
"""


def live_units():
    r = subprocess.run(["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend",
                        LIVE_PREFIX + "-*"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=30, check=False)
    return [line.split()[0] for line in r.stdout.decode().splitlines() if line.split()]


def ended(pid):
    """True when pid is no process, or an ended one (a zombie its new parent has not reaped)."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            text = f.read()
    except OSError:
        return True
    return text[text.rindex(")") + 2] in "ZX"


def wait_for(test, what, timeout=15.0):
    end = time.monotonic() + timeout
    while not test():
        if time.monotonic() > end:
            raise AssertionError("not within %g s: %s" % (timeout, what))
        time.sleep(0.05)


@posix_only("systemd scopes are Linux")
class Live(unittest.TestCase):
    """For real, through the user's systemd: a caller in a scope of its own starts a detached run,
    then the caller's whole scope is killed; the run lives on in its own detach scope."""

    def setUp(self):
        p = mock.patch.dict(os.environ, {contain.PREFIX_ENV: LIVE_PREFIX})
        p.start()
        self.addCleanup(p.stop)
        if ld.scope(dict(os.environ)) is None:
            self.skipTest("no user systemd reachable here")
        self.addCleanup(lambda: wait_for(lambda: not live_units(),
                                         "no unit of %s left (%s)" % (LIVE_PREFIX, live_units())))

    def test_a_detached_run_outlives_its_caller_s_whole_scope_in_a_scope_of_its_own(self):
        d = tempfile.mkdtemp()
        own = os.path.join(d, "pid")
        self.addCleanup(shutil.rmtree, d, True)
        unit = "%s-caller-%d.scope" % (LIVE_PREFIX, os.getpid())
        caller = subprocess.Popen(["systemd-run", "--user", "--scope", "--quiet", "--collect",
                                   "--unit", unit, "--", sys.executable, "-I", "-c", CALLER,
                                   ROOT, own], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: caller.poll() is None and (caller.kill(), caller.wait()))
        line = caller.stdout.readline().split()
        caller.stdout.close()
        self.assertEqual(line[3:], ["None"], line)
        caller_pid, caller_sid, pid = int(line[0]), int(line[1]), int(line[2])
        self.addCleanup(lambda: ended(pid) or kill(pid))
        wait_for(lambda: os.path.exists(own) and os.path.getsize(own), "the run wrote its pid")
        with open(own) as f:
            self.assertEqual(int(f.read()), pid, "create says the run's own pid")
        self.assertRegex(contain.cgroup_of(pid), r"/%s-detach-[^/]+\.scope$" % LIVE_PREFIX)
        self.assertNotEqual(os.getsid(pid), caller_sid, "a session of its own")
        subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", unit], check=True,
                       timeout=30)
        self.assertEqual(caller.wait(15), -signal.SIGKILL, "the caller (pid %d) is killed"
                         % caller_pid)
        time.sleep(0.5)
        self.assertTrue(alive(pid), "the detached run outlives its caller's scope")
        self.assertRegex(contain.cgroup_of(pid), r"/%s-detach-[^/]+\.scope$" % LIVE_PREFIX)
        kill(pid)
        wait_for(lambda: ended(pid), "the run ended")


@windows_only("kitlib.lock imports on Windows only")
class Dispatch(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(kitlib.platform, "NAME", "linux")
        p.start()
        self.addCleanup(p.stop)

    def test_each_call_goes_to_the_linux_backend(self):
        self.assertEqual(dt.command("py", "t", ["run"], "r-1", ["--", "x"]),
                         ld.command("py", "t", ["run"], "r-1", ["--", "x"]))
        self.assertEqual(dt.tool_command("py", "t", ["a"]), ld.tool_command("py", "t", ["a"]))
        self.assertEqual(dt.missing([os.path.abspath("nope"), "-B", "t"]),
                         ld.missing([os.path.abspath("nope"), "-B", "t"]))
        self.assertEqual(dt.wrapper_pid(77), 77)

    def test_create_marks_the_env_then_starts_on_linux(self):
        no_scope(self)
        fake = FakePopen()
        env = {dt.DETACHED_ENV: "1", dt.ORIGIN_ENV: "1:2"}
        self.assertEqual(dt.create(["py", "-B", "t"], "/w", env, run=fake), (4242, None))
        self.assertEqual(fake.kw["env"], {dt.DETACHED_ENV: "1"},
                         "marked: a launcher inside a detached start drops its ancestry")

    def test_windows_keeps_its_carrier(self):
        with mock.patch.object(kitlib.platform, "NAME", "windows"):
            argv = dt.tool_command(sys.executable, "tools/gate.py", ["land"])
        self.assertTrue(argv[0].lower().endswith("pythonw.exe"), argv)
        self.assertEqual(os.path.basename(argv[1]), dt.spawn.CARRIER)
        self.assertEqual(argv[3:], ["-B", os.path.abspath("tools/gate.py"), "land"])


if __name__ == "__main__":
    unittest.main()
