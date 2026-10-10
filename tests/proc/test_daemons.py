"""Tests for kitlib.proc.daemons: which leftovers are build daemons, and that only those are
stopped. Fakes only (the lock test test_release_daemons runs real processes)."""
import unittest

from kitlib.proc import daemons

GRADLE = (r'"C:\jdk\bin\java.exe" -Xmx2g -cp C:\g\gradle-launcher.jar '
          r'org.gradle.launcher.daemon.bootstrap.GradleDaemon 8.10')
KOTLIN = r'java.exe -cp kotlin-compiler.jar org.jetbrains.kotlin.daemon.KotlinCompileDaemon --x'


class LabelOf(unittest.TestCase):
    def test_build_daemons(self):
        for name, cmd, want in (
                ("java.exe", GRADLE, "gradle-daemon"),
                ("java.exe", KOTLIN, "kotlin-daemon"),
                ("dotnet.exe", r'dotnet.exe "C:\sdk\MSBuild.dll" /nodemode:1 /nodeReuse:true',
                 "msbuild-node"),
                ("MSBuild.exe", "MSBuild.exe -nodeMode:1", "msbuild-node"),
                ("VBCSCompiler.exe", "VBCSCompiler.exe -pipename:abc", "compiler-server"),
                ("dotnet.exe", r'dotnet.exe "C:\sdk\Roslyn\bincore\VBCSCompiler.dll" -pipename:x',
                 "compiler-server")):
            self.assertEqual(daemons.label_of(name, cmd), want, cmd)

    def test_anything_else_is_not_one(self):
        for name, cmd in (
                ("python.exe", r"python.exe main.py --listen 127.0.0.1 --port 8188"),
                ("java.exe", "java.exe -jar server.jar nogui"),
                ("dotnet.exe", "dotnet.exe build -c Release"),
                ("MSBuild.exe", "MSBuild.exe proj.sln /m"),
                ("python.exe", "python.exe -m some.tool /nodemode:1"),   # not an msbuild image
                ("java.exe", None), ("", "")):
            self.assertIsNone(daemons.label_of(name, cmd), cmd)


class FindAndStop(unittest.TestCase):
    def test_find_names_each_by_ctime_and_skips_the_gone(self):
        snap = [(10, 1, "java.exe"), (11, 1, "python.exe"), (12, 1, "dotnet.exe")]
        cmds = {10: GRADLE, 11: "python.exe main.py", 12: "dotnet MSBuild.dll /nodemode:1"}
        ctimes = {10: 100, 11: 110}             # 12 is gone
        got = daemons.find([10, 11, 12], snap=snap, cmdline=lambda p, ct: cmds[p],
                           ctime_of=ctimes.get)
        self.assertEqual(got, [(10, 100, "gradle-daemon")])

    def test_stop_ends_each_tree_by_ctime_children_first(self):
        calls, read = [], []

        def descendants(pid, ct, snap):
            read.append((pid, ct, snap))
            return [(11, 110), (12, 120)]       # 11's child 12: parents before children

        def terminate(pid, ct):
            calls.append((pid, ct))
            return "killed"
        out = daemons.stop([(10, 100, "gradle-daemon")], lambda p: "in", terminate=terminate,
                           snapshot=lambda: "SNAP", descendants=descendants,
                           connections=lambda: [])
        self.assertEqual(read, [(10, 100, "SNAP")])
        self.assertEqual(calls, [(12, 120), (11, 110), (10, 100)])
        self.assertEqual(out, [(10, "gradle-daemon", "killed")])

    def test_the_tree_ended_is_the_tree_judged(self):
        """Between the judging and the kill, child 11 exits, a newer process takes pid 11 and
        the daemon starts 13: a tree read again would hold (11, 999) and (13, 130). Exactly the
        judged pairs are ended, so the newer pid 11 is never named with a ctime it has. The
        Windows defaults are patched, so a stop that re-reads the tree through
        process.kill_tree is caught too."""
        from unittest import mock
        from kitlib.proc import process
        reads, calls = [], []

        def descendants(pid, ct, snap=None):
            reads.append(pid)
            return [(11, 110)] if len(reads) == 1 else [(11, 999), (13, 130)]

        def terminate(pid, ct, code=1):
            calls.append((pid, ct))
            return "killed"
        with mock.patch.object(process, "descendants", descendants), \
                mock.patch.object(process, "terminate_verified", terminate):
            out = daemons.stop([(10, 100, "gradle-daemon")], lambda p: "in",
                               snapshot=lambda: [], connections=lambda: [])
        self.assertEqual(calls, [(11, 110), (10, 100)], "the tree was read again to end it")
        self.assertEqual(reads, [10])
        self.assertEqual(out, [(10, "gradle-daemon", "killed")])


LO = b"\x7f\0\0\x01"


def link(a_pid, a_port, b_pid, b_port, addr=LO):
    """The two table rows of one established loopback connection between a_pid and b_pid."""
    a, b = (4, addr, a_port), (4, addr, b_port)
    return [(a, b, a_pid), (b, a, b_pid)]


class StopsOnlyWhatServesTheRun(unittest.TestCase):
    """The releasing run's job holds 10 (a Gradle daemon), its child 11, 12 (a Kotlin daemon)
    and 20 (a client of the run's); 30 is a client in another run's job; 40's job cannot be
    read; 50 is gone; 60's answer is none in_job gives."""
    JOB = {10: "in", 11: "in", 12: "in", 20: "in", 30: "out", 40: "unknown", 50: "gone"}

    def stop(self, conns, found=((10, 100, "gradle-daemon"),), tree=(), trees=None):
        """tree: the descendants of every daemon; trees: {daemon pid: descendants} instead."""
        self.killed = []

        def terminate(pid, ct):
            self.killed.append(pid)
            return "killed"

        def descendants(p, ct, snap):
            return list(trees.get(p, ())) if trees is not None else list(tree)
        return daemons.stop(list(found), self.JOB.get, terminate=terminate,
                            snapshot=lambda: [], descendants=descendants,
                            connections=lambda: conns)

    def test_a_daemon_with_a_client_in_another_runs_job_is_not_stopped(self):
        out = self.stop(link(10, 5000, 30, 6000))
        self.assertEqual(self.killed, [], "a daemon serving another run's client was stopped")
        self.assertEqual(out[0][:2], (10, "gradle-daemon"))
        self.assertTrue(out[0][2].startswith("left: "), out)
        self.assertIn("pid 30", out[0][2])

    def test_a_daemon_serving_only_the_releasing_run_is_stopped(self):
        out = self.stop(link(10, 5000, 20, 6000))
        self.assertEqual(self.killed, [10])
        self.assertEqual(out, [(10, "gradle-daemon", "killed")])

    def test_an_idle_daemon_is_stopped(self):
        self.assertEqual(self.stop([]), [(10, "gradle-daemon", "killed")])

    def test_a_child_serving_another_run_keeps_its_parent(self):
        """Stopping the daemon ends its tree: a Kotlin daemon it started that another run's
        Gradle daemon uses would die with it."""
        out = self.stop(link(11, 5000, 30, 6000), tree=[(11, 101)])
        self.assertEqual(self.killed, [])
        self.assertIn("pid 11 serves pid 30", out[0][2])

    def test_a_peer_whose_job_cannot_be_told_leaves_it(self):
        self.stop(link(10, 5000, 40, 6000))
        self.assertEqual(self.killed, [])

    def test_a_peer_that_is_gone_serves_nobody(self):
        self.stop(link(10, 5000, 50, 6000))
        self.assertEqual(self.killed, [10])

    def test_a_loopback_connection_without_its_other_row_leaves_it(self):
        self.stop(link(10, 5000, 30, 6000)[:1])
        self.assertEqual(self.killed, [])

    def test_a_connection_to_this_machines_own_address_without_its_other_row_leaves_it(self):
        """The remote end is a non-loopback address of this machine (the local end of a row in
        the table): with no row owning it, it is not another host, so the daemon is left.
        why_left, the judge, is asked before stop: its signature predates stop's terminate=, so
        without the fix this test fails on the judgement, not on a keyword stop lacks."""
        here, lan = (4, b"\x0a\0\0\x02", 5000), (4, b"\x0a\0\0\x07", 6000)
        other = ((4, b"\x0a\0\0\x07", 7000), (4, b"\x0a\0\0\x05", 443), 70)
        for conns in ([(here, (4, here[1], 6000), 10)], [(here, lan, 10), other]):
            why = daemons.why_left(10, [], conns, self.JOB.get)
            self.assertIsNotNone(why, "a connection to this machine read as one to another host")
            self.assertIn("other end the table does not show", why)
            out = self.stop(conns)
            self.assertEqual(self.killed, [], conns)
            self.assertIn("other end the table does not show", out[0][2])

    def test_a_connection_to_another_host_is_not_a_client(self):
        far = (4, b"\x0a\0\0\x05", 443)
        self.stop([((4, b"\x0a\0\0\x02", 5000), far, 10)])
        self.assertEqual(self.killed, [10])

    def test_a_connection_inside_its_own_tree_is_its_own(self):
        self.stop(link(10, 5000, 11, 6000), tree=[(11, 101)])
        self.assertEqual(self.killed, [11, 10])

    def test_an_unreadable_table_leaves_it(self):
        def broken():
            raise OSError("no table")
        out = daemons.stop([(10, 100, "gradle-daemon")], self.JOB.get,
                           terminate=lambda *a: self.fail("stopped"), snapshot=lambda: [],
                           descendants=lambda p, ct, snap: [], connections=broken)
        self.assertIn("could not be read", out[0][2])

    def test_pipe_daemons_are_always_left(self):
        out = self.stop([], found=[(10, 100, "msbuild-node"), (12, 120, "compiler-server")])
        self.assertEqual(self.killed, [])
        self.assertTrue(all("named pipes" in r for _p, _l, r in out), out)

    def test_a_peer_with_an_answer_in_job_never_gives_leaves_it(self):
        self.stop(link(10, 5000, 60, 6000))
        self.assertEqual(self.killed, [], "an unexpected in_job answer counted as inside")

    def test_a_daemon_serving_a_left_daemon_is_left_whatever_the_order(self):
        """Another run builds on the run's Gradle daemon 10, which compiles on the run's Kotlin
        daemon 12: 12's only client is in the job, yet ending it breaks the build 10 serves."""
        conns = link(10, 5000, 30, 6000) + link(10, 5001, 12, 7000)
        gradle, kotlin = (10, 100, "gradle-daemon"), (12, 120, "kotlin-daemon")
        for found in ((gradle, kotlin), (kotlin, gradle)):
            out = dict((p, r) for p, _l, r in self.stop(conns, found=found))
            self.assertEqual(self.killed, [], "the daemon a left daemon uses was stopped")
            self.assertIn("serves pid 30", out[10])
            self.assertEqual(out[12], "left: pid 12 serves pid 10 of the gradle-daemon (pid 10), "
                                      "which is left running")

    def test_a_daemon_in_a_left_daemons_tree_is_left(self):
        conns = link(10, 5000, 30, 6000)
        out = self.stop(conns, found=[(12, 120, "kotlin-daemon"), (10, 100, "gradle-daemon")],
                        trees={10: [(12, 120)]})
        self.assertEqual(self.killed, [])
        self.assertEqual(out[0], (12, "kotlin-daemon", "left: it runs in the tree of the "
                                  "gradle-daemon (pid 10), which is left running"))

    def test_a_daemon_with_a_left_daemon_in_its_tree_is_left(self):
        """12 is a pipe daemon (always left) that the Gradle daemon 10 started: ending 10's
        tree would end it."""
        out = self.stop([], found=[(10, 100, "gradle-daemon"), (12, 120, "compiler-server")],
                        trees={10: [(12, 120)]})
        self.assertEqual(self.killed, [])
        self.assertEqual(out[0], (10, "gradle-daemon", "left: ending its tree would end pid 12 "
                                  "of the compiler-server (pid 12), which is left running"))

    def test_an_unreadable_tree_leaves_it(self):
        def broken(p, ct, snap):
            raise OSError("no snapshot")
        out = daemons.stop([(10, 100, "gradle-daemon")], self.JOB.get,
                           terminate=lambda *a: self.fail("stopped"), snapshot=lambda: [],
                           descendants=broken, connections=lambda: [])
        self.assertIn("tree could not be read", out[0][2])

    def test_a_stopped_daemon_and_the_daemon_it_ended_too(self):
        """Both judged on one table before either is ended; the Kotlin daemon in the stopped
        Gradle daemon's tree is ended with it, child first, once, and reports what that kill
        found."""
        out = self.stop(link(10, 5000, 12, 7000), found=[(10, 100, "gradle-daemon"),
                                                         (12, 120, "kotlin-daemon")],
                        trees={10: [(12, 120)]})
        self.assertEqual(self.killed, [12, 10])
        self.assertEqual([r for _p, _l, r in out], ["killed", "killed"])


if __name__ == "__main__":
    unittest.main()
