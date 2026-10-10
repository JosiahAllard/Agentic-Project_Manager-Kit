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

    def test_stop_ends_each_tree_by_ctime(self):
        calls = []

        def kill_tree(pid, ct):
            calls.append((pid, ct))
            return {pid: "killed", pid + 1: "killed"}
        out = daemons.stop([(10, 100, "gradle-daemon")], kill_tree=kill_tree)
        self.assertEqual(calls, [(10, 100)])
        self.assertEqual(out, [(10, "gradle-daemon", "killed")])


if __name__ == "__main__":
    unittest.main()
