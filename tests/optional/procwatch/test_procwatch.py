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
sys.path.insert(0, os.path.join(CODE, '..', '..'))      # the kit root, for kitlib
import procwatch as PW  # noqa: E402

MIN, HOUR = 60, 3600
GIT = r"C:\Program Files\Git\usr\bin"
NOW = 10**17
LIVE_BASH = (50, 1)      # the tool shell's MSYS ids: pid 50, started from Windows (MSYS parent 1)
GONE = 99                # an MSYS parent pid that no longer exists


_SAVED_HOME = {}
_HOME_DIR = None


def setUpModule():
    # procwatch reads the machine config through kitlib (the kit temp folder); under the kit's home
    # fence that needs a temp home, here and in the procwatch processes the tests start.
    global _HOME_DIR
    _HOME_DIR = tempfile.TemporaryDirectory()
    for k in ('USERPROFILE', 'HOME'):
        _SAVED_HOME[k] = os.environ.get(k)
        os.environ[k] = _HOME_DIR.name


def tearDownModule():
    for k, v in _SAVED_HOME.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _HOME_DIR.cleanup()


def proc(pid, ppid, name, cmd="", image=None, age=HOUR, cpu=0.0, ws=10 * 2**20, msys=None):
    """msys = (msys pid, msys parent pid[, tty]) for a Git-Bash program, as Git's ps -l reports it
    (tty `?`: no terminal, as in an agent's tool shell)."""
    return {"pid": pid, "ppid": ppid, "name": name, "cmd": cmd,
            "image": image if image is not None else rf"C:\x\{name}", "ctime": NOW - int(age * 1e7),
            "age": age, "cpu_pct": cpu, "cpu_s": 1.0, "ws": ws, "priv": ws, "handles": 100, "session": 1,
            "mpid": msys[0] if msys else None, "mppid": msys[1] if msys else None,
            "mtty": msys[2] if msys and len(msys) > 2 else None}


NO_TTY = "?"
AGENT_TAIL = "tail.exe -n +1 -F Q:/AgentTemp/claude/Q--repo/1234/tasks/b1.output"    # a Monitor's tail


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
    def test_origin_skips_unlisted_apps(self):
        for app in ("discord.exe", "steam.exe"):
            ps = by_pid(world(proc(700, 4180, app), proc(701, 700, "child.exe")))
            self.assertEqual(ps[701]["origin"], "explorer.exe", app)

    def test_procwatch_origins_env_adds_an_app(self):
        old = os.environ.get("PROCWATCH_ORIGINS")
        os.environ["PROCWATCH_ORIGINS"] = "obs64.exe"
        try:
            ps = by_pid(world(proc(700, 4180, "obs64.exe"), proc(701, 700, "child.exe")))
        finally:
            if old is None:
                del os.environ["PROCWATCH_ORIGINS"]
            else:
                os.environ["PROCWATCH_ORIGINS"] = old
        self.assertEqual(ps[701]["origin"], "obs64.exe")

    def _with_agent_temp(self, reader):
        """Run `reader` as the launcher's AGENT_TEMP reader (kitlib.pmstart.agent_temp_env); the
        kit's own temp folder (work_temp) is set to a different folder so a wrong source shows."""
        import kitlib.config.user as KU
        import kitlib.pmstart as PS
        old = PS.agent_temp_env, KU.work_temp
        PS.agent_temp_env = reader
        KU.work_temp = lambda *a, **k: r"Q:\kit-own-temp"

        def clear():
            for name in ("_agent_temp_claude", "_kit_temp_claude"):     # the resolver is cached
                if hasattr(PW, name):
                    getattr(PW, name).cache_clear()

        def restore():
            PS.agent_temp_env, KU.work_temp = old
            clear()
        self.addCleanup(restore)
        clear()

    def test_agent_script_in_the_agent_temp_folder_is_an_agent_script(self):
        # the agents' TEMP is AGENT_TEMP (kitlib.pmstart.agent_temp_env), not the kit's own temp folder
        calls = []
        self._with_agent_temp(lambda: calls.append(1) or r"Q:\odd-agent-temp")
        ps = by_pid(world(
            proc(900, 77, "python.exe", r"python.exe Q:\odd-agent-temp\claude\x\job.py", age=2 * HOUR),
            # the kit's own temp folder is not where agents run from
            proc(901, 77, "python.exe", r"python.exe Q:\kit-own-temp\claude\x\job.py", age=2 * HOUR),
            *[proc(910 + i, 77, "python.exe", "python.exe job.py") for i in range(5)]))
        self.assertEqual(ps[900]["rule"], "agent-script")
        self.assertNotEqual(ps[901]["rule"], "agent-script")
        self.assertEqual(len(calls), 1)         # resolved once, not once per process

    def test_agent_temp_given_with_forward_slashes_matches_a_backslashed_command_line(self):
        self._with_agent_temp(lambda: "D:/Foo")
        ps = by_pid(world(proc(900, 77, "python.exe", r"python.exe D:\Foo\claude\x\job.py", age=2 * HOUR)))
        self.assertEqual(ps[900]["rule"], "agent-script")

    def test_unset_or_unreadable_agent_temp_keeps_the_rule_off(self):
        def unreadable():
            raise OSError("registry")
        for reader in (lambda: None, lambda: "", unreadable):
            with self.subTest(reader):
                self._with_agent_temp(reader)
                ps = by_pid(world(proc(900, 77, "python.exe", r"python.exe Q:\x\claude\job.py", age=2 * HOUR)))
                self.assertNotEqual(ps[900]["rule"], "agent-script")

    def test_orphaned_tail_follow_and_grep_are_kill_ok_suspects(self):
        # agent-started: no terminal, and the tail follows a file under the agents' temp folder
        ps = by_pid(world(gitp(900, 77, "tail.exe", AGENT_TAIL, 2 * HOUR, (60, GONE, NO_TTY)),
                          gitp(901, 78, "grep.exe", 'grep.exe --line-buffered -E "FAIL"', 2 * HOUR,
                               (61, GONE, NO_TTY))))
        for pid, rule in ((900, "msys-follow"), (901, "msys-filter")):
            self.assertEqual((ps[pid]["verdict"], ps[pid]["rule"], ps[pid]["kill_ok"]), ("SUSPECT", rule, True))

    def test_orphaned_agent_follow_is_kill_ok_for_every_agent_path_form(self):
        self._with_agent_temp(lambda: r"Q:\odd-agent-temp")
        for cmd in (AGENT_TAIL,
                    r"tail.exe -F Q:\AgentTemp\claude\p\s\tasks\b1.output",
                    "tail.exe -F /z/agenttemp/claude/p/s/tasks/b1.output",
                    r"tail.exe -f D:\local\Temp\claude\p\s\tasks\b1.output",   # <TEMP>\claude\
                    "tail.exe -F Q:/GitHub/repo/.claude/worktrees/x/gate.log",
                    r"tail.exe -F Q:\odd-agent-temp\claude\p\x.log",           # the resolved agent temp
                    "tail.exe -F /q/odd-agent-temp/claude/p/x.log",            # ... in MSYS form
                    "tail.exe -F q:/Odd-Agent-Temp/claude/p/x.log"):
            with self.subTest(cmd):
                p = by_pid(world(gitp(900, 77, "tail.exe", cmd, 2 * HOUR, (60, GONE, NO_TTY))))[900]
                self.assertEqual((p["verdict"], p["kill_ok"]), ("SUSPECT", True))

    def test_orphaned_follow_outside_agent_paths_is_reported_not_ended(self):
        for cmd in (r"tail.exe -n +1 -f C:\t\x.output", "tail.exe -F /c/logs/claude.log",
                    "tail.exe -F D:/claudette/x.log"):
            with self.subTest(cmd):
                p = by_pid(world(gitp(900, 77, "tail.exe", cmd, 2 * HOUR, (60, GONE, NO_TTY))))[900]
                self.assertEqual((p["verdict"], p["rule"], p["kill_ok"]), ("SUSPECT", "msys-follow", False))
                self.assertIn("reported only", p["reason"])

    def test_orphaned_follow_with_a_terminal_or_no_tty_known_is_not_ended(self):
        for msys in ((60, GONE, "pty0"), (60, GONE, "cons1"), (60, GONE)):
            with self.subTest(msys):
                p = by_pid(world(gitp(900, 77, "tail.exe", AGENT_TAIL, 2 * HOUR, msys)))[900]
                self.assertEqual((p["verdict"], p["kill_ok"]), ("SUSPECT", False))

    def test_orphaned_filter_or_sleep_with_a_terminal_is_not_ended(self):
        for name, cmd in (("grep.exe", "grep.exe FAIL"), ("sleep.exe", "sleep 9999")):
            for tty, ok in (("pty0", False), (None, False), (NO_TTY, True)):
                with self.subTest((name, tty)):
                    msys = (60, GONE, tty) if tty else (60, GONE)
                    p = by_pid(world(gitp(900, 77, name, cmd, 2 * HOUR, msys)))[900]
                    self.assertEqual((p["verdict"], p["kill_ok"]), ("SUSPECT", ok))

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

    def test_a_plugin_prefixed_agent_name_is_still_the_coordinator(self):
        explorer = proc(4180, 1, "explorer.exe", image=r"C:\Windows\explorer.exe", age=30 * HOUR)
        for agent, want in (("some-plugin:coordinator", "coordinator claude.exe repo-c"),
                            ("some.plugin_2:conductor", "coordinator claude.exe repo-c"),
                            ("some-plugin:reviewer", "claude.exe 503 session")):
            with self.subTest(agent):
                claude = proc(503, 4180, "claude.exe", f'"claude.exe" --agent {agent} -n repo-c', age=8 * HOUR)
                ps = by_pid(PW.classify([explorer, claude, proc(901, 503, "python.exe", "python gate.py", age=MIN)],
                                        3.0))
                self.assertEqual(ps[901]["origin"], want)

    def test_origin_names_the_coordinator(self):
        ps = by_pid(world(proc(900, 501, "python.exe", "python gate.py", age=MIN)))
        self.assertEqual(ps[900]["origin"], "coordinator claude.exe repo-a")
        self.assertEqual(ps[900]["group"], "agent tool commands (parent alive)")

    def test_kill_respects_only_and_reports_results(self):
        ps = world(gitp(900, 77, "tail.exe", AGENT_TAIL, 2 * HOUR, (60, GONE, NO_TTY)),
                   gitp(901, 77, "sleep.exe", "sleep 9999", 2 * HOUR, (61, GONE, NO_TTY)))
        seen = []
        done = PW.kill(ps, only={"msys-sleep"}, terminate=lambda pid, ct: seen.append(pid) or "killed")
        self.assertEqual(seen, [901])
        self.assertEqual(done, [(901, "sleep.exe", "msys-sleep", "killed")])

    def test_kill_passes_the_seen_creation_time(self):
        ps = world(gitp(900, 77, "tail.exe", AGENT_TAIL, 2 * HOUR, (60, GONE, NO_TTY)))
        seen = []
        PW.kill(ps, terminate=lambda pid, ct: seen.append((pid, ct)) or "killed")
        self.assertEqual(seen, [(900, by_pid(ps)[900]["ctime"])])

    def test_kill_skips_an_unknown_creation_time(self):
        ps = world(gitp(900, 77, "tail.exe", AGENT_TAIL, 2 * HOUR, (60, GONE, NO_TTY)))
        by_pid(ps)[900]["ctime"] = None
        self.assertEqual(PW.kill(ps, terminate=lambda *a: self.fail("ended with no creation time")), [])

    def test_kill_rechecks_the_agent_gate(self):
        ps = world(gitp(900, 77, "tail.exe", r"tail.exe -f C:\t\x.output", 2 * HOUR, (60, GONE, NO_TTY)),
                   gitp(901, 77, "sleep.exe", "sleep 9999", 2 * HOUR, (61, GONE, "pty0")))
        for p in ps:
            if p["pid"] in (900, 901):
                p["kill_ok"] = True             # however kill_ok came to be set, kill() asks again
        self.assertEqual(PW.kill(ps, terminate=lambda *a: self.fail("ended a non-agent process")), [])

    def test_kill_live_parent_only_for_root_search(self):
        ps = world(gitp(900, 77, "find.exe", '"C:\\Git\\usr\\bin\\find.exe" / -iname x', 15 * MIN, (60, 50)),
                   gitp(901, 77, "sleep.exe", "sleep 9999", 2 * HOUR, (61, 50)))
        seen = []
        PW.kill(ps, terminate=lambda pid, ct: seen.append(pid) or "killed")
        self.assertEqual(seen, [900])      # root search with a live shell: killed; live-parent sleep: never

    def test_summary_counts(self):
        rows = PW.summarize(world(gitp(900, 77, "tail.exe", AGENT_TAIL, 2 * HOUR, (60, GONE, NO_TTY))))
        sus = [r for r in rows if r["verdict"] == "SUSPECT"]
        self.assertEqual([(r["count"], r["kill_ok"], r["pids"]) for r in sus], [(1, 1, [900])])

    def test_log_line_counts_what_kill_would_end(self):
        import datetime
        procs = world(gitp(900, 77, "tail.exe", AGENT_TAIL, 2 * HOUR, (60, GONE, NO_TTY)),
                      gitp(901, 77, "tail.exe", r"tail.exe -f C:\t\x.output", 2 * HOUR, (61, GONE, NO_TTY)))
        rows = PW.summarize(procs)
        when = datetime.datetime(2026, 10, 20, 11, 0, 0)
        line = PW.log_line(when, procs, rows, "3.3")
        self.assertTrue(line.startswith("2026-10-20T11:00:00 procs=5 suspect=2 pool=3.3G kill_ok=1 "), line)
        self.assertIn("msys-follow=2", line)
        self.assertNotIn("killed=", line)
        self.assertTrue(PW.log_line(when, procs, rows, "3.3", killed=[(900, "tail.exe", "msys-follow", "killed"),
                                                                       (9, "x", "y", "reused")])
                        .endswith(" killed=1"))


class ParsePs(unittest.TestCase):
    """winproc.parse_ps_l: Git's `ps -l` text -> {windows pid: (msys pid, msys parent pid, tty)}."""
    TEXT = ("      PID    PPID    PGID     WINPID   TTY         UID    STIME COMMAND\n"
            "  1563560  320617  320617      50444  ?         197609 11:10:25 /usr/bin/tail\n"
            "S  320617       1  320617      48084  pty0      197609   Oct 15 /usr/bin/bash\n"
            "I     777     776     777       9999  cons1     197609 09:00:00 /usr/bin/sleep\n"
            "garbage line\n")

    def test_reads_ids_and_tty(self):
        import winproc as W
        self.assertEqual(W.parse_ps_l(self.TEXT), {50444: (1563560, 320617, "?"), 48084: (320617, 1, "pty0"),
                                                   9999: (777, 776, "cons1")})

    def test_empty_output_is_an_empty_table(self):
        import winproc as W
        self.assertEqual(W.parse_ps_l(""), {})


@unittest.skipUnless(os.name == "nt", "Windows only")
class MsysTable(unittest.TestCase):
    """winproc.msys_table: a failed ps gives no table, never a partial one."""

    def _run(self, returncode):
        import winproc as W
        from unittest import mock
        out = "  1563560  320617  320617      50444  ?         197609 11:10:25 /usr/bin/tail\n"
        done = subprocess.CompletedProcess(["ps", "-l"], returncode, stdout=out, stderr="")
        with mock.patch("subprocess.run", return_value=done):
            return W.msys_table(ps_exe="ps.exe")

    def test_nonzero_exit_is_no_table(self):
        self.assertIsNone(self._run(1))

    def test_zero_exit_parses_the_rows(self):
        self.assertEqual(self._run(0), {50444: (1563560, 320617, "?")})


class LogPath(unittest.TestCase):
    """--log: a bare flag and a path inside the tool folder both go to <home>/.claude/logs (C3)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.tool = os.path.join(self.tmp.name, "tool")
        os.makedirs(self.tool)
        self.default = os.path.join(self.home, ".claude", "logs", "procwatch.log")
        self._faked_agent_temp(None)                    # the machine's own AGENT_TEMP must not leak in

    def tearDown(self):
        self.tmp.cleanup()

    def test_bare_flag_uses_the_logs_folder(self):
        self.assertEqual(PW.log_path(None, home=self.home, here=self.tool), self.default)

    def _faked_agent_temp(self, value):
        import kitlib.pmstart as PS
        old = PS.agent_temp_env
        PS.agent_temp_env = lambda: value
        self.addCleanup(setattr, PS, "agent_temp_env", old)

    def test_bare_flag_uses_the_agent_temp_folder(self):
        folder = os.path.join(self.tmp.name, "agenttemp")
        self._faked_agent_temp(folder)
        self.assertEqual(PW.log_path(None, home=self.home, here=self.tool), os.path.join(folder, "procwatch.log"))

    def test_bare_flag_falls_back_without_an_agent_temp_folder(self):
        self._faked_agent_temp(None)
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


class FakeWinproc:
    """A stand-in for winproc in collect(): pid 10 is listed by the snapshot but has exited by the
    time collect reads it (a short-lived process that took a dead parent's pid); pid 20 is that
    dead parent's child."""
    CT = NOW - int(HOUR * 1e7)

    def snapshot(self):
        return [(10, 4, "git.exe"), (20, 10, "python.exe")]

    def cpu_sample(self, pids):
        return {}

    def sleep(self, s):
        pass

    def filetime_now(self):
        return NOW

    def msys_table(self):
        return None

    def details(self, pid):
        d = {"ctime": None, "cpu": None, "image": None, "cmd": None, "handles": None, "ws": None,
             "priv": None, "gone": False}
        if pid == 10:
            d["gone"] = True
        else:
            d.update(ctime=self.CT, cpu=0.0, image=r"C:\x\python.exe", cmd="python.exe x.py")
        return d


class CollectRace(unittest.TestCase):
    def test_a_process_gone_before_it_is_read_is_not_listed_so_its_child_is_an_orphan(self):
        saved = sys.modules.get("winproc")
        sys.modules["winproc"] = FakeWinproc()
        try:
            procs = PW.classify(PW.collect(sample_s=0), 0)
        finally:
            if saved is None:
                sys.modules.pop("winproc", None)
            else:
                sys.modules["winproc"] = saved
        self.assertEqual([p["pid"] for p in procs], [20])
        self.assertFalse(procs[0]["parent_alive"])


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
        # The parent exits, but this Popen keeps its process handle open until the test ends, so
        # Windows cannot give its pid to another process. With the handle closed (subprocess.run),
        # a busy machine could reuse the pid for a process collect cannot read (it exits before it
        # is read, or belongs to another user); an unreadable parent counts as alive, and the
        # orphan was once reported with a live parent.
        parent = subprocess.Popen([sys.executable, "-c", spawner, pidfile])
        self.assertEqual(parent.wait(30), 0)
        with open(pidfile) as f:
            orphan = int(f.read())
        os.remove(pidfile)
        try:
            procs = PW.collect(sample_s=0.5)
            self.assertNotIn(parent.pid, {p["pid"] for p in procs})     # exited, and its pid still held
            mine = [p for p in procs if p["pid"] == orphan]
            self.assertEqual(len(mine), 1)
            self.assertEqual(mine[0]["ppid"], parent.pid)
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
            del parent                  # closes the held handle

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
            self.assertIsNotNone(p["mtty"], "Git's ps -l TTY column was not read")
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

    def test_bare_log_flag_writes_in_the_agent_temp_folder(self):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, USERPROFILE=home, HOME=home, AGENT_TEMP=home)
            r = subprocess.run([sys.executable, os.path.join(CODE, "procwatch.py"), "--sample", "0.2", "--log"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120, env=env)
            self.assertIn(r.returncode, (0, 1), r.stderr)
            with open(os.path.join(home, "procwatch.log"), encoding="utf-8") as f:
                self.assertIn(" procs=", f.read())

    def test_bare_log_flag_writes_under_the_profile_logs_folder(self):
        import kitlib.pmstart as PS
        if PS.agent_temp_env():
            self.skipTest("an agent temp folder is set here; the fallback is covered by LogPath")
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, USERPROFILE=home, HOME=home)
            r = subprocess.run([sys.executable, os.path.join(CODE, "procwatch.py"), "--sample", "0.2", "--log"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120, env=env)
            self.assertIn(r.returncode, (0, 1), r.stderr)
            with open(os.path.join(home, ".claude", "logs", "procwatch.log"), encoding="utf-8") as f:
                self.assertIn(" procs=", f.read())


if __name__ == "__main__":
    unittest.main()
