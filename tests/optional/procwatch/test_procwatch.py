"""Tests for procwatch.py. The classifier tests use made-up process lists; the live tests (Windows)
start and stop only their own processes.

    python -m unittest test_procwatch -v
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(HERE, '..', '..', '..', 'optional', 'procwatch')
sys.path.insert(0, CODE)
sys.path.insert(0, HERE)
import procwatch as PW  # noqa: E402

MIN, HOUR = 60, 3600
GIT = r"C:\Program Files\Git\usr\bin"
NOW = 10**17
LIVE_BASH = (50, 1)      # the tool shell's MSYS ids: pid 50, started from Windows (MSYS parent 1)
GONE = 99                # an MSYS parent pid that no longer exists


def proc(pid, ppid, name, cmd="", image=None, age=HOUR, cpu=0.0, ws=10 * 2**20, msys=None):
    """msys = (msys pid, msys parent pid) for a Git-Bash program, as Git's ps -l reports it."""
    return {"pid": pid, "ppid": ppid, "name": name, "cmd": cmd,
            "image": image if image is not None else rf"C:\x\{name}", "ctime": NOW - int(age * 1e7),
            "age": age, "cpu_pct": cpu, "cpu_s": 1.0, "ws": ws, "priv": ws, "handles": 100, "session": 1,
            "mpid": msys[0] if msys else None, "mppid": msys[1] if msys else None}


def gitp(pid, ppid, name, cmd, age, msys):
    return proc(pid, ppid, name, cmd, GIT + "\\" + name, age, msys=msys)


def world(*extra):
    base = [proc(4180, 1, "explorer.exe", image=r"C:\Windows\explorer.exe", age=30 * HOUR),
            proc(500, 4180, "claude.exe", '"claude.exe" --agent coordinator -n repo-a', age=8 * HOUR),
            gitp(501, 500, "bash.exe", "bash -c ...", 6 * HOUR, LIVE_BASH)]
    return PW.classify(base + list(extra), 3.0)


def by_pid(procs):
    return {p["pid"]: p for p in procs}


class Classify(unittest.TestCase):
    def test_orphaned_tail_follow_and_grep_are_kill_ok_suspects(self):
        ps = by_pid(world(gitp(900, 77, "tail.exe", r"tail.exe -n +1 -f C:\t\x.output", 2 * HOUR, (60, GONE)),
                          gitp(901, 78, "grep.exe", 'grep.exe --line-buffered -E "FAIL"', 2 * HOUR, (61, GONE))))
        for pid, rule in ((900, "msys-follow"), (901, "msys-filter")):
            self.assertEqual((ps[pid]["verdict"], ps[pid]["rule"], ps[pid]["kill_ok"]), ("SUSPECT", rule, True))

    def test_live_parent_or_young_means_expected(self):
        ps = by_pid(world(gitp(900, 501, "tail.exe", "tail.exe -f x", 2 * HOUR, (60, 50)),
                          gitp(901, 77, "tail.exe", "tail.exe -f x", 2 * MIN, (61, GONE))))
        self.assertEqual(ps[900]["verdict"], "EXPECTED")
        self.assertEqual(ps[901]["verdict"], "EXPECTED")          # dead parent but under the age floor

    def test_git_bash_program_with_a_dead_windows_parent_but_live_msys_parent_is_not_an_orphan(self):
        # Git-Bash runs a program through a fork that exits at once: Windows parent 77 is gone,
        # MSYS parent 50 (the live tool shell) is not.
        ps = by_pid(world(gitp(900, 77, "tail.exe", "tail.exe -f x", 2 * HOUR, (60, 50))))
        self.assertTrue(ps[900]["parent_alive"])
        self.assertEqual(ps[900]["verdict"], "EXPECTED")
        self.assertEqual(ps[900]["origin"], "coordinator claude.exe repo-a")

    def test_without_the_msys_table_git_bash_programs_are_never_orphans(self):
        ps = by_pid(world(gitp(900, 77, "tail.exe", "tail.exe -f x", 2 * HOUR, None)))
        self.assertTrue(ps[900]["parent_alive"])
        self.assertFalse(ps[900]["kill_ok"])

    def test_reused_parent_pid_counts_as_dead(self):
        # pid 4180 exists but is younger than the child: the child's real parent is gone.
        ps = by_pid(world(proc(900, 4180, "testhost.exe", "testhost.exe --port 1", age=40 * HOUR)))
        self.assertFalse(ps[900]["parent_alive"])
        self.assertEqual(ps[900]["verdict"], "SUSPECT")

    def test_root_search(self):
        f = f'"{GIT}\\find.exe" '
        ps = by_pid(world(gitp(900, 77, "find.exe", f + "/ -name foo", 5 * MIN, (60, 50)),
                          gitp(901, 77, "find.exe", f + "/c -name foo", 5 * MIN, (61, GONE)),
                          gitp(902, 77, "find.exe", f + "/c/GitHub/x -name foo", 5 * MIN, (62, GONE)),
                          gitp(903, 77, "find.exe", f + "C:\\ -iname x", 5 * MIN, (63, GONE)),
                          gitp(904, 77, "find.exe", f + "/ -iname bunit.core.dll", 15 * MIN, (64, 50))))
        self.assertEqual((ps[900]["verdict"], ps[900]["kill_ok"]), ("SUSPECT", False))   # live shell, young: report only
        self.assertEqual((ps[904]["verdict"], ps[904]["kill_ok"]), ("SUSPECT", True))    # live shell, >= 10 min: kill
        self.assertEqual((ps[901]["verdict"], ps[901]["kill_ok"]), ("SUSPECT", True))
        self.assertNotEqual(ps[902]["rule"], "root-search")                             # scoped search
        self.assertEqual(ps[903]["rule"], "root-search")

    def test_msbuild_nodes(self):
        node = '"dotnet.exe" "C:\\sdk\\MSBuild.dll" /nologo /nodemode:1 /nodeReuse:{} /low:false'
        ps = by_pid(world(proc(900, 77, "dotnet.exe", node.format("true"), age=2 * HOUR),
                          proc(901, 77, "dotnet.exe", node.format("true"), age=2 * HOUR, cpu=40.0),
                          proc(902, 77, "dotnet.exe", node.format("true"), age=5 * MIN),
                          proc(903, 501, "dotnet.exe", node.format("false"), age=2 * HOUR),
                          proc(904, 77, "dotnet.exe", node.format("false"), age=10 * MIN)))
        self.assertEqual((ps[900]["verdict"], ps[900]["kill_ok"]), ("SUSPECT", True))    # idle, orphaned, old
        self.assertEqual(ps[901]["verdict"], "EXPECTED")                                  # busy: in a build
        self.assertEqual(ps[902]["verdict"], "EXPECTED")                                  # under 15 min
        self.assertEqual(ps[903]["verdict"], "EXPECTED")                                  # its build is alive
        self.assertEqual(ps[904]["verdict"], "SUSPECT")                                   # reuse off, build gone

    def test_daemons_are_never_kill_ok(self):
        gd = "java.exe -cp gradle-daemon-main.jar org.gradle.launcher.daemon.bootstrap.GradleDaemon 9.7.1"
        ps = world(*[proc(900 + i, 77, "java.exe", gd, age=4 * HOUR) for i in range(5)],
                   proc(950, 77, "VBCSCompiler.exe", "VBCSCompiler.exe -pipename:x", age=10 * HOUR),
                   proc(951, 77, "java.exe", "java.exe org.jetbrains.kotlin.daemon.KotlinCompileDaemon", age=3 * HOUR))
        daemons = [p for p in ps if p["rule"] in ("gradle-daemon", "roslyn-server", "kotlin-daemon")]
        self.assertEqual(len(daemons), 7)
        self.assertTrue(all(p["verdict"] == "SUSPECT" for p in daemons if p["rule"] != "roslyn-server"))
        self.assertFalse(any(p["kill_ok"] for p in daemons))
        self.assertEqual(by_pid(ps)[950]["verdict"], "EXPECTED")      # one compiler server is normal

    def test_protected_is_never_touched(self):
        # a repo's own production process is protected through PROCWATCH_PROTECT-style extra patterns
        extra = [r"\\apps\\myapp\\live\\", r"run-prod\.ps1"]
        PW.PROTECT_PATTERNS.extend(extra)
        self.addCleanup(lambda: [PW.PROTECT_PATTERNS.remove(x) for x in extra])
        ps = by_pid(world(
            proc(900, 77, "Prod.Brain.exe", '"D:\\apps\\myapp\\live\\brain\\Prod.Brain.exe"'),
            proc(901, 77, "java.exe", "java -jar D:\\apps\\myapp\\live\\worker\\w.jar", cpu=300),
            proc(902, 77, "powershell.exe", "powershell -File C:\\x\\run-prod.ps1"),
            gitp(903, 77, "tail.exe", "tail -f D:\\apps\\myapp\\live\\log.txt", HOUR, (60, GONE)),
            proc(904, 77, "python.exe", "C:\\ComfyUI\\ComfyUI\\.venv\\Scripts\\python.exe main.py", cpu=500)))
        for pid in range(900, 905):
            self.assertEqual((ps[pid]["verdict"], ps[pid]["kill_ok"]), ("EXPECTED", False), pid)
            self.assertTrue(ps[pid]["group"].startswith("protected"))
        self.assertEqual(PW.kill(list(ps.values()), terminate=lambda *a: self.fail("killed protected")), [])

    def test_burner_signature_and_runaway(self):
        burn = [proc(900 + i, 501, "python.exe", "python burn.py", age=5 * MIN, cpu=99.0) for i in range(10)]
        ps = by_pid(world(*burn, proc(950, 501, "node.exe", "node spin.js", age=2 * HOUR, cpu=100.0),
                          proc(951, 501, "node.exe", "node quick.js", age=5 * MIN, cpu=100.0)))
        self.assertTrue(all(ps[900 + i]["rule"] == "cpu-burner" for i in range(10)))
        self.assertEqual(ps[950]["rule"], "cpu-runaway")
        self.assertEqual(ps[951]["verdict"], "EXPECTED")
        self.assertFalse(any(p["kill_ok"] for p in ps.values() if p["rule"] in ("cpu-burner", "cpu-runaway")))

    def test_old_agent_name_is_still_the_coordinator(self):
        old = proc(502, 4180, "claude.exe", '"claude.exe" --agent conductor -n repo-b', age=8 * HOUR)
        ps = by_pid(PW.classify([proc(4180, 1, "explorer.exe", image=r"C:\Windows\explorer.exe", age=30 * HOUR), old,
                                 proc(901, 502, "python.exe", "python gate.py", age=MIN)], 3.0))
        self.assertEqual(ps[901]["origin"], "coordinator claude.exe repo-b")

    def test_origin_names_the_coordinator(self):
        ps = by_pid(world(proc(900, 501, "python.exe", "python gate.py", age=MIN)))
        self.assertEqual(ps[900]["origin"], "coordinator claude.exe repo-a")
        self.assertEqual(ps[900]["group"], "agent tool commands (parent alive)")

    def test_kill_respects_only_and_reports_results(self):
        ps = world(gitp(900, 77, "tail.exe", "tail -f a", 2 * HOUR, (60, GONE)),
                   gitp(901, 77, "sleep.exe", "sleep 9999", 2 * HOUR, (61, GONE)))
        seen = []
        done = PW.kill(ps, only={"msys-sleep"}, terminate=lambda pid, ct: seen.append(pid) or "killed")
        self.assertEqual(seen, [901])
        self.assertEqual(done, [(901, "sleep.exe", "msys-sleep", "killed")])

    def test_kill_live_parent_only_for_root_search(self):
        ps = world(gitp(900, 77, "find.exe", '"C:\\Git\\usr\\bin\\find.exe" / -iname x', 15 * MIN, (60, 50)),
                   gitp(901, 77, "sleep.exe", "sleep 9999", 2 * HOUR, (61, 50)))
        seen = []
        PW.kill(ps, terminate=lambda pid, ct: seen.append(pid) or "killed")
        self.assertEqual(seen, [900])      # root search with a live shell: killed; live-parent sleep: never

    def test_summary_counts(self):
        rows = PW.summarize(world(gitp(900, 77, "tail.exe", "tail -f a", 2 * HOUR, (60, GONE))))
        sus = [r for r in rows if r["verdict"] == "SUSPECT"]
        self.assertEqual([(r["count"], r["kill_ok"], r["pids"]) for r in sus], [(1, 1, [900])])


class LogPath(unittest.TestCase):
    """--log: a bare flag and a path inside the tool folder both go to <home>/.claude/logs (C3)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.tool = os.path.join(self.tmp.name, "tool")
        os.makedirs(self.tool)
        self.default = os.path.join(self.home, ".claude", "logs", "procwatch.log")

    def tearDown(self):
        self.tmp.cleanup()

    def test_bare_flag_uses_the_logs_folder(self):
        self.assertEqual(PW.log_path(None, home=self.home, here=self.tool), self.default)

    def test_a_path_in_the_tool_folder_is_moved(self):
        inside = os.path.join(self.tool, "procwatch.log")
        self.assertEqual(PW.log_path(inside, home=self.home, here=self.tool), self.default)

    def test_any_other_path_is_kept(self):
        other = os.path.join(self.tmp.name, "elsewhere", "pw.log")
        self.assertEqual(PW.log_path(other, home=self.home, here=self.tool), other)
        sibling = self.tool + "-x.log"                  # shares a prefix, not inside
        self.assertEqual(PW.log_path(sibling, home=self.home, here=self.tool), sibling)

    @unittest.skipUnless(os.name == "nt", "junctions are Windows only")
    def test_a_path_through_a_link_onto_the_tool_folder_is_moved(self):
        link = os.path.join(self.tmp.name, "linked")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, self.tool], capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("mklink /J failed here: " + r.stdout + r.stderr)
        try:
            inside = os.path.join(link, "procwatch.log")
            self.assertEqual(PW.log_path(inside, home=self.home, here=self.tool), self.default)
        finally:
            os.rmdir(link)                              # removes the junction, not its target


def _terminate(W, pid):
    h = W.open_process(pid, W.PROCESS_TERMINATE)
    if h:
        W.K32.TerminateProcess(h, 1)
        W.close(h)


@unittest.skipUnless(os.name == "nt", "Windows only")
class GitPs(unittest.TestCase):
    """GIT_PS_EXE replaces Git for Windows' default ps.exe locations."""

    def test_env_overrides_the_defaults(self):
        import winproc as W
        self.assertEqual(W.git_ps({"GIT_PS_EXE": "elsewhere/ps.exe"}), ["elsewhere/ps.exe"])

    def test_unset_or_empty_keeps_the_defaults(self):
        import winproc as W
        for env in ({}, {"GIT_PS_EXE": ""}):
            self.assertEqual(W.git_ps(env), W.GIT_PS_DEFAULT)


@unittest.skipUnless(os.name == "nt", "Windows only")
class Live(unittest.TestCase):
    def test_collect_sees_an_orphan_and_kill_terminates_only_it(self):
        import winproc as W
        tag = f"procwatch-selftest-{os.getpid()}-{time.time_ns()}"
        pidfile = os.path.join(tempfile.gettempdir(), tag + ".pid")
        spawner = (f"import subprocess,sys; g=subprocess.Popen([sys.executable,'-c',"
                   f"'import time; time.sleep(120)  # {tag}'], stdin=subprocess.DEVNULL, "
                   f"stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); open(sys.argv[1],'w').write(str(g.pid))")
        subprocess.run([sys.executable, "-c", spawner, pidfile], check=True, timeout=30)   # parent exits
        with open(pidfile) as f:
            orphan = int(f.read())
        os.remove(pidfile)
        try:
            procs = PW.collect(sample_s=0.5)
            mine = [p for p in procs if p["pid"] == orphan]
            self.assertEqual(len(mine), 1)
            self.assertIn(tag, mine[0]["cmd"])
            rule = ("selftest", "self-test orphan", lambda p: tag in (p.get("cmd") or ""),
                    lambda p, ctx: "self-test orphan" if not p["parent_alive"] else None, True)
            saved = PW.RULES[:]
            PW.RULES.insert(0, rule)
            try:
                PW.classify(procs, 0.5)
            finally:
                PW.RULES[:] = saved
            p = mine[0]
            self.assertFalse(p["parent_alive"])
            self.assertEqual((p["verdict"], p["kill_ok"]), ("SUSPECT", True))
            self.assertEqual(W.terminate_verified(orphan, p["ctime"] + 1), "reused")   # ctime mismatch refused
            done = PW.kill(procs, only={"selftest"})
            self.assertEqual(done, [(orphan, "python.exe", "selftest", "killed")])
        finally:
            _terminate(W, orphan)

    def test_a_live_git_bash_program_is_not_called_an_orphan(self):
        import winproc as W
        bash = next((p for p in (GIT + r"\bash.exe",) if os.path.exists(p)), None)
        if not bash or W.msys_table() is None:
            self.skipTest("needs Git Bash and its ps")
        secs = f"{40 + (os.getpid() % 1000) / 1000:.3f}"          # a sleep length only this test uses
        sh = subprocess.Popen([bash, "-c", f"/usr/bin/sleep {secs}; true"])
        try:
            for _ in range(50):
                procs = PW.collect(sample_s=0.2)
                mine = [p for p in procs if p["name"].lower() == "sleep.exe" and secs in (p["cmd"] or "")]
                if mine and mine[0]["mpid"] is not None:
                    break
            self.assertEqual(len(mine), 1)
            PW.classify(procs, 0.2)
            p = mine[0]
            self.assertIsNotNone(p["mpid"])
            self.assertTrue(p["parent_alive"], "a running Git-Bash child was reported as an orphan")
        finally:
            for q in procs:
                if q["name"].lower() == "sleep.exe" and secs in (q["cmd"] or ""):
                    _terminate(W, q["pid"])
            sh.kill()
            sh.wait(10)

    def test_report_runs_and_exits_0_or_1(self):
        r = subprocess.run([sys.executable, os.path.join(CODE, "procwatch.py"), "--sample", "0.5"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
        self.assertIn(r.returncode, (0, 1), r.stderr)
        self.assertIn("processes", r.stdout)

    def test_bare_log_flag_writes_under_the_profile_logs_folder(self):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, USERPROFILE=home, HOME=home)
            r = subprocess.run([sys.executable, os.path.join(CODE, "procwatch.py"), "--sample", "0.2", "--log"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120, env=env)
            self.assertIn(r.returncode, (0, 1), r.stderr)
            with open(os.path.join(home, ".claude", "logs", "procwatch.log"), encoding="utf-8") as f:
                self.assertIn(" procs=", f.read())


if __name__ == "__main__":
    unittest.main()
